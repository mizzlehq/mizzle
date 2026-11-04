"""Source generation: ``pull`` (snapshot -> schema module) and ``typegen`` (TypedDicts)."""

from __future__ import annotations

import datetime
import decimal
import ipaddress
import keyword
import re
import uuid
from collections.abc import Mapping, Sequence

from mizzle import pg
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table
from mizzle.core.schema.view import View
from mizzle.core.snapshot import (
    ColumnSnap,
    GrantsSnap,
    IndexSnap,
    PolicySnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    ViewSnap,
    default_cost,
    key,
    sequence_defaults,
)

# Names a class body calls (text(), sql(...), one(...)): an attribute of the same name would
# shadow them for the rest of the class, so such columns get a trailing "_" (and name=).
_RESERVED_ATTRS = {"config", "from_db", "sql", "one", "many", "Column", "Any", *pg.__all__}

_SIMPLE_TYPES = {
    "integer": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "text": "text",
    "boolean": "boolean",
    "real": "real",
    "double precision": "double_precision",
    "date": "date",
    "uuid": "uuid",
    "bytea": "bytea",
    "inet": "inet",
    "interval": "interval",
    "serial": "serial",
    "bigserial": "bigserial",
    "smallserial": "smallserial",
    "numeric": "numeric",
    "character varying": "varchar",
    "character": "char",
    "tsvector": "tsvector",
    "tsquery": "tsquery",
    "cidr": "cidr",
    "macaddr": "macaddr",
    "macaddr8": "macaddr8",
    "bit": "bit",
    "bit varying": "varbit",
    "xml": "xml",
    "money": "money",
    "point": "point",
    "line": "line",
    "lseg": "lseg",
    "box": "box",
    "path": "path",
    "polygon": "polygon",
    "circle": "circle",
    "hstore": "hstore",
    "citext": "citext",
    "ltree": "ltree",
    "int4range": "int4range",
    "int8range": "int8range",
    "numrange": "numrange",
    "tsrange": "tsrange",
    "tstzrange": "tstzrange",
    "daterange": "daterange",
    "int4multirange": "int4multirange",
    "int8multirange": "int8multirange",
    "nummultirange": "nummultirange",
    "tsmultirange": "tsmultirange",
    "tstzmultirange": "tstzmultirange",
    "datemultirange": "datemultirange",
    "vector": "vector",
    "halfvec": "halfvec",
    "sparsevec": "sparsevec",
}


def _identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = f"c_{ident}"
    if keyword.iskeyword(ident) or ident in _RESERVED_ATTRS:
        ident += "_"
    return ident


def _class_name(name: str) -> str:
    # Upper-case only the first letter of each part: NotificationStatus stays as it is.
    parts = re.split(r"[^a-zA-Z0-9]+", name)
    return "".join(part[0].upper() + part[1:] for part in parts if part) or "T"


def _words(name: str) -> list[str]:
    """``teamMember``, ``team_member`` and ``TeamMember`` -> ``["team", "member"]``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", spaced) if w]


def _styled(words: list[str], camel: bool) -> str:
    if not camel:
        return "_".join(words)
    return words[0] + "".join(w.capitalize() for w in words[1:]) if words else ""


def _plural(word: str) -> str:
    """Simple English plural; names that already end in s are kept (users, posts)."""
    if word.endswith("s"):
        return word
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def _singular(word: str) -> str:
    """Undo _plural for table names (users -> user, categories -> category, status kept)."""
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if re.search(r"(x|z|ch|sh)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _is_camel(columns: Sequence[str]) -> bool:
    """Whether a table names its columns in camelCase (createdAt) rather than snake_case."""
    return any(re.search(r"[a-z][A-Z]", c) for c in columns) and not any("_" in c for c in columns)


# A foreign key column named after what it points to: userId, user_id, userID -> "user".
_FK_SUFFIX = re.compile(r"^(.*[a-zA-Z0-9])(?:_id|Id|ID)$")


def _type_ident(name: str) -> str:
    """A name as format_type() prints it: quoted only when necessary."""
    return name if re.fullmatch(r"[a-z_][a-z0-9_$]*", name) else '"' + name.replace('"', '""') + '"'


def _sql(text: str) -> str:
    """A ``sql("...")`` call; without values, sql() takes its text literally."""
    return f"sql({text!r})"


class _SchemaWriter:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot: Snapshot = snapshot
        self.pg_imports: set[str] = set()
        self.enum_classes: dict[str, str] = {}  # db type name -> class name
        self.enum_values: dict[str, list[str]] = {}  # db type name -> values
        self.enum_names: dict[str, tuple[str, str]] = {}  # db type name -> (schema, name)
        self.domains: dict[str, str] = {}  # column type -> Python type for pg_domain()
        self.other_imports: set[str] = set()  # e.g. "import decimal" for a domain's type
        self.table_classes: dict[str, str] = {}  # "schema.table" -> class name
        self.attrs: dict[str, dict[str, str]] = {}  # "schema.table" -> column -> attribute
        self.uses_column: bool = False
        self.uses_any: bool = False
        self.uses_relations: set[str] = set()
        self.relations: dict[str, list[str]] = {}  # "schema.table" -> relation lines

    def attr(self, column: str) -> str:
        """A column's attribute name; an enum class used in the class body is not shadowed."""
        name = _identifier(column)
        return f"{name}_" if name in self.enum_classes.values() else name

    def factory(self, sql_type: str, kwargs: str) -> str:
        if sql_type.endswith("[]"):
            self.pg_imports.add("array")
            item = self.factory(sql_type[:-2], "")
            return f"array({item}{', ' + kwargs if kwargs else ''})"
        if sql_type in self.enum_classes:
            self.pg_imports.add("pg_enum")
            cls = self.enum_classes[sql_type]
            schema, name = self.enum_names[sql_type]
            extra = f", schema={schema!r}" if schema != "public" else ""
            extra += f", {kwargs}" if kwargs else ""
            return f"pg_enum({cls}, type_name={name!r}{extra})"
        if sql_type in self.domains:
            self.pg_imports.add("pg_domain")
            py = self.domains[sql_type]
            return f"pg_domain({sql_type!r}, {py}{', ' + kwargs if kwargs else ''})"
        if sql_type in ("jsonb", "json"):
            self.pg_imports.add(sql_type)
            self.uses_any = True
            return f"{sql_type}[Any]({kwargs})"
        if match := re.fullmatch(
            r"(timestamp|time)(?:\((\d+)\))? (with|without) time zone", sql_type
        ):
            fn = match.group(1)
            self.pg_imports.add(fn)
            args = ["tz=True"] if match.group(3) == "with" else []
            if match.group(2) is not None:
                args.append(f"precision={match.group(2)}")
            return f"{fn}({', '.join([*args, kwargs] if kwargs else args)})"
        if match := re.fullmatch(
            r"(character varying|character|numeric|vector|halfvec|sparsevec|bit|varbit|bit varying)"
            r"\(([\d,]+)\)",
            sql_type,
        ):
            fn = _SIMPLE_TYPES[match.group(1)]
            self.pg_imports.add(fn)
            numbers = match.group(2).replace(",", ", ")
            return f"{fn}({numbers}{', ' + kwargs if kwargs else ''})"
        if sql_type in _SIMPLE_TYPES:
            fn = _SIMPLE_TYPES[sql_type]
            self.pg_imports.add(fn)
            return f"{fn}({kwargs})"
        self.uses_column = True
        self.uses_any = True
        extra = f", {kwargs}" if kwargs else ""
        return f"Column[Any]({sql_type!r}, None{extra})"

    def column(self, table: TableSnap, column: ColumnSnap, attr: str) -> str:
        kwargs = f"name={column['name']!r}" if attr != column["name"] else ""
        code = self.factory(column["type"], kwargs)
        if collation := column.get("collation"):
            code += f".collate({collation!r})"
        if comment := column.get("comment"):
            code += f".comment({comment!r})"
        tname, cname = table["name"], column["name"]
        pk = table["primary_key"]
        is_pk = pk is not None and pk["columns"] == [cname] and pk["name"] == f"{tname}_pkey"
        identity = column.get("identity")
        if is_pk and pk is not None:
            pk_opts = ", ".join(
                f"{opt}=True" for opt in ("deferrable", "initially_deferred") if pk.get(opt)
            )
            code += f".primary_key({pk_opts})" if pk_opts else ".primary_key()"
        elif (
            column["not_null"]
            and identity is None
            and column["type"] not in ("serial", "bigserial", "smallserial")
        ):
            code += ".not_null()"
        if identity is not None:
            fn = "always" if identity["always"] else "by_default"
            seq: list[str] = []
            increment = identity["increment"]
            if increment != 1:
                seq.append(f"increment={increment}")
            low, high = sequence_defaults(column["type"], increment)
            low, high = identity.get("min_value", low), identity.get("max_value", high)
            if identity["start"] != (low if increment > 0 else high):
                seq.append(f"start={identity['start']}")
            for option, value in (
                ("min_value", identity.get("min_value")),
                ("max_value", identity.get("max_value")),
                ("cache", identity.get("cache")),
                ("cycle", identity.get("cycle")),
            ):
                if value is not None:
                    seq.append(f"{option}={value!r}")
            code += f".generated_{fn}_as_identity({', '.join(seq)})"
        if generated := column.get("generated"):
            stored = "" if generated["stored"] else ", stored=False"
            code += f".generated_always_as({_sql(generated['expression'])}{stored})"
        unique = table["uniques"].get(f"{tname}_{cname}_key")
        if unique is not None and unique["columns"] == [cname]:
            u_opts: list[str] = []
            if unique.get("nulls_not_distinct"):
                u_opts.append("nulls_not_distinct=True")
            u_opts += [
                f"{opt}=True" for opt in ("deferrable", "initially_deferred") if unique.get(opt)
            ]
            code += f".unique({', '.join(u_opts)})" if u_opts else ".unique()"
        if column["default"] is not None:
            code += f".default({self.default(column)})"
        fk = table["foreign_keys"].get(f"{tname}_{cname}_fkey")
        if fk is not None and fk["columns"] == [cname] and len(fk["ref_columns"]) == 1:
            target = self.table_classes.get(f"{fk['ref_schema']}.{fk['ref_table']}")
            if target is not None:
                ref_attr = self.attrs[f"{fk['ref_schema']}.{fk['ref_table']}"][fk["ref_columns"][0]]
                options = "".join(
                    f", {opt}={fk[opt]!r}" for opt in ("on_delete", "on_update") if fk[opt]
                )
                options += "".join(
                    f", {opt}=True"
                    for opt in ("deferrable", "initially_deferred", "not_valid")
                    if fk.get(opt)
                )
                code += f".references(lambda: {target}.{ref_attr}{options})"
        return code

    def default(self, column: ColumnSnap) -> str:
        """An enum member for a literal enum default ('UNREAD'), else raw SQL."""
        default = column["default"]
        assert default is not None
        cls = self.enum_classes.get(column["type"])
        match = re.fullmatch(r"'((?:[^']|'')*)'(?:::.+)?", default)
        if cls is not None and match is not None:
            value = match.group(1).replace("''", "'")
            if value in self.enum_values[column["type"]]:
                return f"{cls}.{_identifier(value).upper()}"
        return _sql(default)

    def add_relations(self) -> None:
        """``one()`` for every foreign key to a pulled table, and the reverse ``many()``
        (or ``one()`` when the key columns are unique) on the table it points to.

        Names are guesses from the key column (userId -> user) and the referencing table
        (comment -> comments); rename freely, queries only depend on the columns."""
        tables = self.snapshot["tables"]
        taken = {k: {*self.attrs[k].values(), *_RESERVED_ATTRS} for k in tables}
        camel = {k: _is_camel(list(t["columns"])) for k, t in tables.items()}

        def claim(key: str, name: str, fallback: str) -> str:
            name = _identifier(name)
            for candidate in (
                name,
                _identifier(
                    f"{name}_{fallback}"
                    if not camel[key]
                    else name + fallback[:1].upper() + fallback[1:]
                ),
            ):
                if candidate not in taken[key]:
                    taken[key].add(candidate)
                    return candidate
            n = 2
            while f"{name}{n}" in taken[key]:
                n += 1
            taken[key].add(f"{name}{n}")
            return f"{name}{n}"

        links = [
            (k, fk)
            for k, t in tables.items()
            for fk in t["foreign_keys"].values()
            if f"{fk['ref_schema']}.{fk['ref_table']}" in self.table_classes
        ]
        pair_count: dict[tuple[str, str], int] = {}
        for k, fk in links:
            pair = (k, f"{fk['ref_schema']}.{fk['ref_table']}")
            pair_count[pair] = pair_count.get(pair, 0) + 1

        for k, fk in links:
            target = f"{fk['ref_schema']}.{fk['ref_table']}"
            source_cls, target_cls = self.table_classes[k], self.table_classes[target]
            source_words = _words(tables[k]["name"])
            source_words[-1] = _singular(source_words[-1])
            target_words = _words(tables[target]["name"])
            target_words[-1] = _singular(target_words[-1])
            single = fk["columns"][0] if len(fk["columns"]) == 1 else None
            stem = _FK_SUFFIX.match(single) if single else None
            # userId -> user; a key column without an Id suffix (author) is itself a column
            # name, so claim() falls back to author_user.
            one_name = claim(
                k,
                stem.group(1) if stem else (single or _styled(target_words, camel[k])),
                _styled(target_words, camel[k]),
            )
            ambiguous = pair_count[(k, target)] > 1 or k == target
            rel = f", relation_name={one_name!r}" if ambiguous else ""
            fields = ", ".join(f"{source_cls}.{self.attrs[k][c]}" for c in fk["columns"])
            refs = ", ".join(f"{target_cls}.{self.attrs[target][c]}" for c in fk["ref_columns"])
            self.relations.setdefault(k, []).append(
                f"{one_name} = one(lambda: {target_cls}, fields=lambda: [{fields}], "
                f"references=lambda: [{refs}]{rel})"
            )

            key_columns = sorted(fk["columns"])
            table = tables[k]
            pk = table["primary_key"]
            unique = (
                (pk is not None and sorted(pk["columns"]) == key_columns)
                or any(sorted(u["columns"]) == key_columns for u in table["uniques"].values())
                or any(
                    i["unique"]
                    and not i["where"]
                    and "expression" not in i
                    and sorted(i["columns"]) == key_columns
                    for i in table["indexes"].values()
                )
            )
            back = source_words if unique else [*source_words[:-1], _plural(source_words[-1])]
            if ambiguous:
                back = [*back, "by", *_words(one_name)]
            back_name = claim(
                target, _styled(back, camel[target]), _styled(source_words, camel[target])
            )
            kind = "one" if unique else "many"
            self.uses_relations |= {"one", kind}
            self.relations.setdefault(target, []).append(
                f"{back_name} = {kind}(lambda: {source_cls}{rel})"
            )

    def config_items(self, table: TableSnap, attrs: dict[str, str]) -> list[str]:
        tname = table["name"]
        items: list[str] = []

        def cols(names: Sequence[str]) -> str:
            return ", ".join(f"cls.{attrs[n]}" for n in names)

        def flags(snap: Mapping[str, object], names: Sequence[str]) -> str:
            return "".join(f", {n}=True" for n in names if snap.get(n))

        pk = table["primary_key"]
        if pk is not None and not (len(pk["columns"]) == 1 and pk["name"] == f"{tname}_pkey"):
            defer = flags(pk, ("deferrable", "initially_deferred"))
            items.append(f"primary_key({cols(pk['columns'])}, name={pk['name']!r}{defer})")
        for u in table["uniques"].values():
            if not (len(u["columns"]) == 1 and u["name"] == f"{tname}_{u['columns'][0]}_key"):
                flag = ", nulls_not_distinct=True" if u.get("nulls_not_distinct") else ""
                flag += flags(u, ("deferrable", "initially_deferred"))
                items.append(f"unique({cols(u['columns'])}, name={u['name']!r}{flag})")
        for fk in table["foreign_keys"].values():
            col = fk["columns"][0]
            if len(fk["columns"]) == 1 and fk["name"] == f"{tname}_{col}_fkey":
                continue  # written as .references()
            ref_key = f"{fk['ref_schema']}.{fk['ref_table']}"
            target = self.table_classes.get(ref_key)
            if target is None:
                # Names come from the database: repr() keeps them on one escaped line.
                items.append(f"# foreign key {fk['name']!r} references {ref_key!r}, not pulled")
                continue
            refs = ", ".join(f"{target}.{self.attrs[ref_key][c]}" for c in fk["ref_columns"])
            options = "".join(f", {o}={fk[o]!r}" for o in ("on_delete", "on_update") if fk[o])
            options += flags(fk, ("deferrable", "initially_deferred", "not_valid"))
            items.append(
                f"foreign_key([{cols(fk['columns'])}], lambda: [{refs}], "
                f"name={fk['name']!r}{options})"
            )
        for c in table["checks"].values():
            not_valid = ", not_valid=True" if c.get("not_valid") else ""
            items.append(f"check({c['name']!r}, {_sql(c['expression'])}{not_valid})")
        for e in table.get("exclusions", {}).values():
            keys: list[str] = []
            for el in e["elements"]:
                column = el.get("column")
                key = (
                    f"cls.{attrs[column]}" if column is not None else _sql(el.get("expression", ""))
                )
                keys.append(f"({key}, {el['operator']!r})")
            options = f", using={e['method']!r}"
            if where := e.get("where"):
                options += f", where={_sql(where)}"
            options += flags(e, ("deferrable", "initially_deferred"))
            items.append(f"exclusion({', '.join(keys)}, name={e['name']!r}{options})")
        for pname, bound in table.get("partitions", {}).items():
            items.append(f"partition({pname!r}, for_values={bound!r})")
        items += [_index_item(i, attrs) for i in table["indexes"].values()]
        if rls := table.get("rls"):
            args = [] if rls["enabled"] else ["enabled=False"]
            items.append(f"rls({', '.join([*args, 'force=True'] if rls['force'] else args)})")
        items += [_policy_item(p) for p in table.get("policies", {}).values()]
        items += _grant_items(table.get("grants", {}))
        return items

    def view(self, k: str, cls: str) -> str:
        view = self.snapshot.get("views", {})[k]
        kw = f"name={view['name']!r}"
        if view["schema"] != "public":
            kw += f", schema={view['schema']!r}"
        if view["materialized"]:
            kw += ", materialized=True"
        attrs = {c: self.attr(c) for c in view["columns"]}
        lines = ["@final", f"class {cls}(View, {kw}):"]
        for cname, column in view["columns"].items():
            kwargs = f"name={cname!r}" if attrs[cname] != cname else ""
            lines.append(f"    {attrs[cname]} = {self.factory(column['type'], kwargs)}")
        lines += [
            "",
            "    @classmethod",
            "    @override",
            "    def query(cls, db: QueryBuilder):",
            f"        return {_sql(view['definition'])}",
        ]
        items = [_index_item(i, attrs) for i in view.get("indexes", {}).values()]
        items += _grant_items(view.get("grants", {}))
        if items:
            body = "".join(f"            {i},\n" for i in items)
            lines += [
                "",
                "    @classmethod",
                "    @override",
                "    def config(cls) -> list[Constraint]:",
                "        return [\n" + body + "        ]",
            ]
        return "\n".join(lines)

    def render(self) -> str:
        enums_code: list[str] = []
        for enum in self.snapshot["enums"].values():
            cls = _class_name(enum["name"])
            if cls in self.enum_classes.values():
                cls = _class_name(f"{enum['schema']}_{enum['name']}")
            sql_type = _type_ident(enum["name"])  # as format_type() prints it
            if enum["schema"] != "public":
                sql_type = f"{_type_ident(enum['schema'])}.{sql_type}"
            self.enum_classes[sql_type] = cls
            self.enum_values[sql_type] = list(enum["values"])
            self.enum_names[sql_type] = (enum["schema"], enum["name"])
            members = [f"    {_identifier(v).upper()} = {v!r}" for v in enum["values"]]
            enums_code.append(f"class {cls}(enum.StrEnum):\n" + "\n".join(members))

        domains_code: list[str] = []
        for domain in self.snapshot.get("domains", {}).values():
            schema, name = domain["schema"], domain["name"]
            sql_type = _type_ident(name)
            if schema != "public":
                sql_type = f"{_type_ident(schema)}.{sql_type}"
            py, stmt = _domain_py(domain["base_type"])
            if stmt == "from typing import Any":
                self.uses_any = True
            elif stmt is not None:
                self.other_imports.add(stmt)
            self.domains[sql_type] = py
            args = [repr(name)]
            if schema != "public":
                args.append(f"schema={schema!r}")
            args.append(f"as_={domain['base_type']!r}")
            if default := domain.get("default"):
                args.append(f"default={default!r}")
            if domain.get("not_null"):
                args.append("not_null=True")
            args += [f"check={check!r}" for check in domain.get("checks", [])]
            if collation := domain.get("collation"):
                args.append(f"collation={collation!r}")
            domains_code.append(f"d_{_identifier(name)} = domain({', '.join(args)})")

        for k, table in self.snapshot["tables"].items():
            name = _class_name(table["name"])
            if name in self.table_classes.values() or name in self.enum_classes.values():
                name = _class_name(f"{table['schema']}_{table['name']}")
            self.table_classes[k] = name
            self.attrs[k] = {c: self.attr(c) for c in table["columns"]}
        self.add_relations()

        objects_code: list[str] = []
        function_vars: dict[str, str] = {}  # "schema.name" -> variable, for triggers
        taken_vars: set[str] = set()
        for ext in self.snapshot.get("extensions", {}).values():
            schema = ext.get("schema", "public")
            extra = f", schema={schema!r}" if schema != "public" else ""
            var = f"ext_{_identifier(ext['name'])}"
            objects_code.append(f"{var} = extension({ext['name']!r}{extra})")
        for fn in self.snapshot.get("functions", {}).values():
            var = f"fn_{_identifier(fn['name'])}"
            n = 2
            while var in taken_vars:  # overloads
                var, n = f"fn_{_identifier(fn['name'])}_{n}", n + 1
            taken_vars.add(var)
            k = key(fn["schema"], fn["name"])
            if k not in function_vars or not fn["args"]:  # triggers run the one without args
                function_vars[k] = var
            args = [repr(fn["name"]), f"returns={fn['returns']!r}", f"body={fn['body']!r}"]
            if fn["language"] != "plpgsql":
                args.append(f"language={fn['language']!r}")
            if fn["args"]:
                args.append(f"args={fn['args']!r}")
            if fn["schema"] != "public":
                args.append(f"schema={fn['schema']!r}")
            if fn["volatility"] != "volatile":
                args.append(f"volatility={fn['volatility']!r}")
            if fn["security_definer"]:
                args.append("security_definer=True")
            args += [f"{flag}=True" for flag in ("strict", "leakproof") if fn.get(flag)]
            if parallel := fn.get("parallel"):
                args.append(f"parallel={parallel!r}")
            if (cost := fn.get("cost")) is not None and cost != default_cost(fn["language"]):
                args.append(f"cost={cost!r}")
            if settings := fn.get("settings"):
                args.append(f"settings={settings!r}")
            objects_code.append(f"{var} = function(\n    " + ",\n    ".join(args) + ",\n)")
        uses_objects = {
            name
            for name, used in (
                ("extension", self.snapshot.get("extensions")),
                ("function", self.snapshot.get("functions")),
            )
            if used
        }
        if domains_code:
            uses_objects.add("domain")

        tables_code: list[str] = []
        uses_config: set[str] = set()
        has_config = False
        for k, table in self.snapshot["tables"].items():
            attrs = self.attrs[k]
            kw = f"name={table['name']!r}"
            if table["schema"] != "public":
                kw += f", schema={table['schema']!r}"
            if comment := table.get("comment"):
                kw += f", comment={comment!r}"
            if table.get("unlogged"):
                kw += ", unlogged=True"
            if options := table.get("options"):
                kw += f", storage={options!r}"
            if partition_by := table.get("partition_by"):
                kw += f", partition_by={partition_by!r}"
            lines = ["@final", f"class {self.table_classes[k]}(Table, {kw}):"]
            for cname, column in table["columns"].items():
                lines.append(f"    {attrs[cname]} = {self.column(table, column, attrs[cname])}")
            if relations := self.relations.get(k):
                # Forward one(..., fields=...) first, then the reverse sides.
                relations = sorted(relations, key=lambda r: "fields=" not in r)
                lines += ["", *(f"    {r}" for r in relations)]
            items = self.config_items(table, attrs)
            if items:
                for maker in (
                    "primary_key",
                    "unique",
                    "foreign_key",
                    "check",
                    "exclusion",
                    "partition",
                    "unique_index",
                    "index",
                    "rls",
                    "policy",
                    "grant",
                ):
                    if any(i.startswith(f"{maker}(") for i in items):
                        uses_config.add(maker)
                body = "".join(
                    f"            {i}\n" if i.startswith("#") else f"            {i},\n"
                    for i in items
                )
                has_config = True
                lines += [
                    "",
                    "    @classmethod",
                    "    @override",
                    "    def config(cls) -> list[Constraint]:",
                    "        return [\n" + body + "        ]",
                ]
            tables_code.append("\n".join(lines))

        taken = {*self.table_classes.values(), *self.enum_classes.values()}
        owners: list[tuple[str, TableSnap | ViewSnap, dict[str, str]]] = [
            (self.table_classes[k], t, self.attrs[k]) for k, t in self.snapshot["tables"].items()
        ]
        for k, view in self.snapshot.get("views", {}).items():
            cls = _class_name(view["name"])
            if cls in taken:
                cls = _class_name(f"{view['schema']}_{view['name']}_view")
            taken.add(cls)
            tables_code.append(self.view(k, cls))
            owners.append((cls, view, {c: self.attr(c) for c in view["columns"]}))
            for i in view.get("indexes", {}).values():
                uses_config.add("unique_index" if i["unique"] else "index")
                has_config = True
            if view.get("grants"):
                uses_config.add("grant")
                has_config = True

        triggers_code: list[str] = []
        for cls, owner, attrs in owners:
            for t in owner.get("triggers", {}).values():
                var = f"trg_{_identifier(owner['name'])}_{_identifier(t['name'])}"
                if t["function"] not in function_vars:
                    triggers_code.append(
                        f"# trigger {t['name']!r} runs {t['function']!r}, which was not pulled"
                    )
                    continue
                uses_objects.add("trigger")
                triggers_code.append(
                    f"{var} = trigger(\n    "
                    + ",\n    ".join(_trigger_args(t, cls, attrs, function_vars[t["function"]]))
                    + ",\n)"
                )

        header = ['"""Generated by `mizzle pull`. Review before committing."""', ""]
        if enums_code:
            header.append("import enum")
        header.extend(sorted(self.other_imports))
        typing_names: set[str] = {"final"}
        views = bool(self.snapshot.get("views"))
        if self.uses_any:
            typing_names.add("Any")
        if has_config or views:  # config() and query() methods are marked @override
            typing_names.add("override")
        header.append(f"from typing import {', '.join(sorted(typing_names))}")
        header.append("")
        blocks = [*enums_code, *domains_code, *objects_code, *tables_code, *triggers_code]
        body = "\n\n\n".join(blocks)
        mizzle_names: set[str] = {
            "Table",
            *(["sql"] if re.search(r"\bsql\(", body) else []),  # unused imports fail strict checks
            *uses_config,
            *self.uses_relations,
            *uses_objects,
        }
        if views:
            mizzle_names |= {"View", "QueryBuilder"}
        if self.uses_column:
            mizzle_names.add("Column")
        if has_config:
            mizzle_names.add("Constraint")
        header.append(f"from mizzle import {', '.join(sorted(mizzle_names))}")
        if self.pg_imports:
            header.append(f"from mizzle.pg import {', '.join(sorted(self.pg_imports))}")
        return "\n".join(header) + "\n\n\n" + body + "\n"


def _trigger_args(t: TriggerSnap, cls: str, attrs: dict[str, str], execute: str) -> list[str]:
    args = [
        repr(t["name"]),
        f"on={cls}",
        f"execute={execute}",
        f"timing={t['timing']!r}",
        f"events={t['events']!r}",
    ]
    if t["for_each"] != "row":
        args.append(f"for_each={t['for_each']!r}")
    if t["when"] is not None:
        args.append(f"when={_sql(t['when'])}")
    if columns := t.get("columns"):
        args.append(f"update_of=[{', '.join(f'{cls}.{attrs[c]}' for c in columns)}]")
    for side, name in (("old_table", t.get("old_table")), ("new_table", t.get("new_table"))):
        if name is not None:
            args.append(f"{side}={name!r}")
    return args


def _grant_items(grants: GrantsSnap) -> list[str]:
    """``grant([...], to=[...])``, one per set of privileges."""
    by_privileges: dict[tuple[str, ...], list[str]] = {}
    for role, privileges in grants.items():
        by_privileges.setdefault(tuple(privileges), []).append(role)
    return [
        f"grant({list(privileges)!r}, to={sorted(roles)!r})"
        for privileges, roles in by_privileges.items()
    ]


def _index_item(i: IndexSnap, attrs: dict[str, str]) -> str:
    fn = "unique_index" if i["unique"] else "index"
    flag = ", nulls_not_distinct=True" if i.get("nulls_not_distinct") else ""
    keys = (
        _sql(i["expression"])
        if "expression" in i
        else ", ".join(f"cls.{attrs[n]}" for n in i["columns"])
    )
    code = f"{fn}({i['name']!r}{flag})"
    opclasses = i.get("opclasses", [])
    opclass: str | list[str | None] | None = opclasses[0] if opclasses else None
    if len(set(opclasses)) > 1:
        opclass = opclasses
    if "method" in i or opclass:
        method = i.get("method", "btree")
        code += f".using({method!r}{f', {opclass!r}' if opclass else ''})"
    code += f".on({keys})"
    if storage := i.get("options"):
        code += f".with_({', '.join(f'{k}={v!r}' for k, v in storage.items())})"
    if i["where"]:
        code += f".where({_sql(i['where'])})"
    return code


def _policy_item(p: PolicySnap) -> str:
    args = [repr(p["name"])]
    if p["command"] != "all":
        args.append(f"for_={p['command']!r}")
    if p["roles"] != ["public"]:
        args.append(f"to={p['roles']!r}")
    if p["using"] is not None:
        args.append(f"using={_sql(p['using'])}")
    if p["with_check"] is not None:
        args.append(f"with_check={_sql(p['with_check'])}")
    if not p["permissive"]:
        args.append('as_="restrictive"')
    return f"policy({', '.join(args)})"


def render_schema_module(snapshot: Snapshot) -> str:
    return _SchemaWriter(snapshot).render()


_DOMAIN_PY: dict[str, tuple[str, str | None]] = {
    "text": ("str", None),
    "character varying": ("str", None),
    "character": ("str", None),
    "name": ("str", None),
    "smallint": ("int", None),
    "integer": ("int", None),
    "bigint": ("int", None),
    "boolean": ("bool", None),
    "real": ("float", None),
    "double precision": ("float", None),
    "numeric": ("decimal.Decimal", "import decimal"),
    "date": ("datetime.date", "import datetime"),
    "timestamp without time zone": ("datetime.datetime", "import datetime"),
    "timestamp with time zone": ("datetime.datetime", "import datetime"),
    "time without time zone": ("datetime.time", "import datetime"),
    "time with time zone": ("datetime.time", "import datetime"),
    "interval": ("datetime.timedelta", "import datetime"),
    "uuid": ("uuid.UUID", "import uuid"),
    "bytea": ("bytes", None),
    "json": ("Any", "from typing import Any"),
    "jsonb": ("Any", "from typing import Any"),
}


def _domain_py(base_type: str) -> tuple[str, str | None]:
    name = re.sub(r"\(.*\)", "", base_type.removesuffix("[]")).strip()
    return _DOMAIN_PY.get(name, ("Any", "from typing import Any"))


# -- typegen --------------------------------------------------------------------------

_PY_TYPES: dict[type[object], str] = {
    int: "int",
    str: "str",
    bool: "bool",
    float: "float",
    bytes: "bytes",
    decimal.Decimal: "decimal.Decimal",
    datetime.datetime: "datetime.datetime",
    datetime.date: "datetime.date",
    datetime.time: "datetime.time",
    datetime.timedelta: "datetime.timedelta",
    uuid.UUID: "uuid.UUID",
    ipaddress.IPv4Address: "ipaddress.IPv4Address",
    ipaddress.IPv6Address: "ipaddress.IPv6Address",
    ipaddress.IPv4Interface: "ipaddress.IPv4Interface",
    ipaddress.IPv6Interface: "ipaddress.IPv6Interface",
    ipaddress.IPv4Network: "ipaddress.IPv4Network",
    ipaddress.IPv6Network: "ipaddress.IPv6Network",
}


_VECTOR = re.compile(r"(?:vector|halfvec)\(\d+\)")


def _py_type(column: ColumnBase, imports: set[str]) -> str:
    if column.enum is not None:
        imports.add(f"from {column.enum.__module__} import {column.enum.__qualname__}")
        base = column.enum.__qualname__
        if column.sql_type.endswith("[]"):
            base = f"list[{base}]"
    elif _VECTOR.fullmatch(column.sql_type):
        base = "list[float]"
    elif column.sql_type.startswith("sparsevec("):
        base = "dict[int, float]"
    elif column.accepts is None or list in column.accepts:
        base = "list[Any]" if column.accepts else "Any"
    else:
        # The first accepted type is the column's own; extra ones (int for float) are leniency.
        base = _PY_TYPES.get(column.accepts[0], "Any")
    for module in ("decimal", "datetime", "uuid", "ipaddress"):
        if base.startswith(f"{module}."):
            imports.add(f"import {module}")
    return f"{base} | None" if column.nullable else base


def render_types_module(tables: Sequence[type[Table]]) -> str:
    """TypedDicts for a table's values: ``<Name>Row`` for a full selected row (including
    generated columns), and ``<Name>Insert`` / ``<Name>Update`` for writes. Use them with
    ``db.insert(Users).values(UsersInsert(...))`` and to name the shape a select returns.

    Views are left out: read them through queries, never write them."""
    imports: set[str] = set()
    blocks: list[str] = []
    for table in tables:
        if issubclass(table, View):  # read through queries, never written
            continue
        columns = table.__table__.columns
        row = [f"class {table.__name__}Row(TypedDict):"]
        insert = [f"class {table.__name__}Insert(TypedDict):"]
        update = [f"class {table.__name__}Update(TypedDict, total=False):"]
        for attr, column in columns.items():
            py = _py_type(column, imports)
            row.append(f"    {attr}: {py}")
            if column.read_only:  # generated columns: the database writes them
                continue
            insert.append(
                f"    {attr}: {py if column.required_on_insert else f'NotRequired[{py}]'}"
            )
            update.append(f"    {attr}: {py}")
        blocks += ["\n".join(row), "\n".join(insert), "\n".join(update)]
    body = "\n\n\n".join(blocks)
    # Only what the body uses: strict checkers flag unused imports.
    typing = [n for n in ("Any", "NotRequired") if re.search(rf"\b{n}\b", body)]
    head = [
        '"""Generated by `mizzle typegen`. Do not edit; rerun `mizzle typegen` instead."""',
        "",
        *sorted(i for i in imports if i.startswith("import ")),
        f"from typing import {', '.join([*typing, 'TypedDict'])}",
        *sorted(i for i in imports if i.startswith("from ")),
    ]
    return "\n".join(head) + "\n\n\n" + body + "\n"
