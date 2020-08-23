from datasette.app import Datasette
from datasette_graphql.utils import _schema_cache
import json
import pathlib
import pytest
import re
import httpx
from .fixtures import ds, db_path, db_path2


@pytest.mark.asyncio
async def test_plugin_is_installed():
    app = Datasette([], memory=True).app()
    async with httpx.AsyncClient(app=app) as client:
        response = await client.get("http://localhost/-/plugins.json")
        assert 200 == response.status_code
        installed_plugins = {p["name"] for p in response.json()}
        assert "datasette-graphql" in installed_plugins


@pytest.mark.asyncio
async def test_graphiql():
    app = Datasette([], memory=True).app()
    async with httpx.AsyncClient(app=app) as client:
        response = await client.get(
            "http://localhost/graphql", headers={"Accept": "text/html"}
        )
        assert 200 == response.status_code
        assert "<title>GraphiQL</title>" in response.text


@pytest.mark.asyncio
async def test_query_fields(ds):
    query = """
    {
        __schema {
            queryType {
                fields {
                    name
                }
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 200
        fields = {
            f["name"]
            for f in response.json()["data"]["__schema"]["queryType"]["fields"]
        }
        assert fields == {
            "_1_images_row",
            "_1_images",
            "t_table_",
            "t_table__row",
            "issues_row",
            "issues",
            "licenses_row",
            "licenses",
            "repos_row",
            "repos",
            "table_with_compound_pk_row",
            "table_with_compound_pk",
            "table_with_pk_row",
            "table_with_pk",
            "table_with_rowid_row",
            "table_with_rowid",
            "type_compound_key",
            "type_compound_key_row",
            "users_row",
            "users",
            "view_on_table_with_pk_row",
            "view_on_table_with_pk",
            "view_on_repos_row",
            "view_on_repos",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected_errors",
    [
        # Search fails on table that doesn't support it
        (
            """{
                users(search:"cleopaws") {
                    nodes {
                        name
                    }
                }
            }""",
            [
                {
                    "message": 'Unknown argument "search" on field "users" of type "Query".',
                    "locations": [{"line": 2, "column": 23}],
                }
            ],
        ),
    ],
)
async def test_graphql_errors(ds, query, expected_errors):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 500
        assert response.json()["errors"] == expected_errors


graphql_re = re.compile(r"```graphql(.*?)```", re.DOTALL)
json_re = re.compile(r"```json\n(.*?)```", re.DOTALL)
variables_re = re.compile(r"```json\+variables\n(.*?)```", re.DOTALL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", (pathlib.Path(__file__).parent.parent / "examples").glob("*.md")
)
async def test_graphql_examples(ds, path):
    content = path.read_text()
    query = graphql_re.search(content)[1]
    try:
        variables = variables_re.search(content)[1]
    except TypeError:
        variables = "{}"
    expected = json.loads(json_re.search(content)[1])
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post(
            "http://localhost/graphql",
            json={"query": query, "variables": json.loads(variables),},
        )
        assert response.status_code == 200, response.json()
        if response.json()["data"] != expected:
            print("Actual:")
            print(json.dumps(response.json()["data"], indent=4))
        assert response.json()["data"] == expected


@pytest.mark.asyncio
async def test_graphql_error(ds):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post(
            "http://localhost/graphql",
            json={
                "query": """{
                    users {
                        nodes {
                            nam2
                            score
                        }
                    }
                }"""
            },
        )
        assert response.status_code == 500
        assert response.json() == {
            "data": None,
            "errors": [
                {
                    "message": 'Cannot query field "nam2" on type "users". Did you mean "name"?',
                    "locations": [{"line": 4, "column": 29}],
                }
            ],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "on,expected",
    [
        (True, {"testTable": {"nodes": [{"fullName": "This is a full name"}]}}),
        (False, {"test_table": {"nodes": [{"full_name": "This is a full name"}]}}),
    ],
)
async def test_graphql_auto_camelcase(db_path2, on, expected):
    _schema_cache.clear()
    ds = Datasette(
        [db_path2], metadata={"plugins": {"datasette-graphql": {"auto_camelcase": on}}}
    )
    query = """
    {
        TABLE {
            nodes {
                NAME
            }
        }
    }
    """.replace(
        "NAME", "fullName" if on else "full_name"
    ).replace(
        "TABLE", "testTable" if on else "test_table"
    )
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 200
        assert response.json() == {"data": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["table_with_pk", "table_with_rowid", "table_with_compound_pk"]
)
async def test_graphql_pagination(ds, table):
    # Every table should have 21 items, so should paginate 3 times
    after = None
    names_from_nodes = []
    names_from_edges = []
    while True:
        args = ["first: 10"]
        if after:
            args.append('after: "{}"'.format(after))
        query = """
        {
            TABLE(ARGS) {
                totalCount
                pageInfo {
                    endCursor
                    hasNextPage
                }
                nodes {
                    name
                }
                edges {
                    node {
                        name
                    }
                }
            }
        }
        """.replace(
            "TABLE", table
        ).replace(
            "ARGS", ", ".join(args)
        )
        async with httpx.AsyncClient(app=ds.app()) as client:
            response = await client.post(
                "http://localhost/graphql", json={"query": query}
            )
            assert response.status_code == 200
            data = response.json()["data"]
            names_from_nodes.extend([n["name"] for n in data[table]["nodes"]])
            names_from_edges.extend([e["node"]["name"] for e in data[table]["edges"]])
            after = data[table]["pageInfo"]["endCursor"]
            assert data[table]["pageInfo"]["hasNextPage"] == bool(after)
            assert data[table]["totalCount"] == 21
            if not after:
                break
    assert len(names_from_nodes) == 21
    assert len(names_from_edges) == 21
    assert len(set(names_from_nodes)) == 21
    assert len(set(names_from_edges)) == 21


@pytest.mark.asyncio
async def test_graphql_multiple_databases(db_path, db_path2):
    ds = Datasette([db_path, db_path2])
    query = """
    {
        test_table {
            nodes {
                full_name
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post(
            "http://localhost/graphql/test2", json={"query": query}
        )
        assert response.status_code == 200, response.json()
        assert response.json() == {
            "data": {"test_table": {"nodes": [{"full_name": "This is a full name"}]}}
        }


@pytest.mark.asyncio
async def test_graphql_json_columns(db_path):
    _schema_cache.clear()
    ds = Datasette(
        [db_path],
        metadata={
            "databases": {
                "test": {
                    "tables": {
                        "repos": {
                            "plugins": {"datasette-graphql": {"json_columns": ["tags"]}}
                        }
                    }
                }
            }
        },
    )
    query = """
    {
        repos {
            nodes {
                full_name
                tags
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 200, response.json()
        assert response.json() == {
            "data": {
                "repos": {
                    "nodes": [
                        {
                            "full_name": "simonw/datasette",
                            "tags": ["databases", "apis"],
                        },
                        {"full_name": "cleopaws/dogspotter", "tags": ["dogs"]},
                        {"full_name": "simonw/private", "tags": []},
                    ]
                }
            }
        }


@pytest.mark.asyncio
async def test_graphql_output_schema(ds):
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.options("http://localhost/graphql/test.graphql")
        assert response.status_code == 200
        for fragment in (
            "schema {\n  query: Query\n}",
            "input IntegerOperations {",
            "users(filter: [usersFilter], where: String, first: Int, after: String, sort: usersSort, sort_desc: usersSortDesc): usersCollection",
            "users_row(filter: [usersFilter], where: String, after: String, sort: usersSort, sort_desc: usersSortDesc, id: Int): users",
            "type _1_images {",
            "type _1_imagesCollection {",
            "type _1_imagesEdge {",
            "input _1_imagesFilter {",
            "enum _1_imagesSort {",
            "enum _1_imagesSortDesc {",
        ):
            assert fragment in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cors_enabled", [True, False])
async def test_cors_headers(db_path, cors_enabled):
    ds = Datasette([db_path], cors=cors_enabled,)
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.options("http://localhost/graphql")
        assert response.status_code == 200
        desired_headers = {
            "access-control-allow-headers": "content-type",
            "access-control-allow-method": "POST",
            "access-control-allow-origin": "*",
        }.items()
        if cors_enabled:
            assert desired_headers <= dict(response.headers).items()
        else:
            assert not desired_headers <= dict(response.headers).items()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name,expected_status,expected_data",
    [
        ("Q1", 200, {"data": {"users_row": {"name": "cleopaws"}}}),
        ("Q2", 200, {"data": {"users_row": {"id": 1}}}),
        (
            "",
            500,
            {
                "data": None,
                "errors": [
                    {
                        "message": "Must provide operation name if query contains multiple operations."
                    }
                ],
            },
        ),
    ],
)
async def test_operation_name(ds, operation_name, expected_status, expected_data):
    query = """
    query Q1 {
        users_row {
            name
        }
    }
    query Q2 {
        users_row {
            id
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post(
            "http://localhost/graphql",
            json={"query": query, "operationName": operation_name},
        )
        assert response.status_code == expected_status, response.json()
        assert response.json() == expected_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,extra_query_string,expected_data",
    [
        # Regular query
        (
            """
            {
                users_row {
                    name
                }
            }
            """,
            {},
            {"data": {"users_row": {"name": "cleopaws"}}},
        ),
        # operationName
        (
            """
            query Q1 {
                users_row {
                    name
                }
            }
            query Q2 {
                users_row {
                    id
                }
            }
            """,
            {"operationName": "Q2"},
            {"data": {"users_row": {"id": 1}}},
        ),
        # variables
        (
            """
            query specific_repo($name: String) {
                repos(filter: {name: {eq: $name}}) {
                    nodes {
                        name
                    }
                }
            }
            """,
            {"variables": json.dumps({"name": "datasette"})},
            {"data": {"repos": {"nodes": [{"name": "datasette"}]}}},
        ),
    ],
)
async def test_graphql_http_get(ds, query, extra_query_string, expected_data):
    async with httpx.AsyncClient(app=ds.app()) as client:
        params = dict(extra_query_string)
        params["query"] = query
        response = await client.get("http://localhost/graphql", params=params)
        assert response.status_code == 200
        assert response.json() == expected_data


@pytest.mark.asyncio
async def test_configured_fts_search_for_view(db_path):
    _schema_cache.clear()
    ds = Datasette(
        [db_path],
        metadata={
            "databases": {
                "test": {
                    "tables": {
                        "view_on_repos": {"fts_table": "repos_fts", "fts_pk": "id"}
                    }
                }
            }
        },
    )
    query = """
    {
        view_on_repos(search: "dogspotter") {
            nodes {
                id
                full_name
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 200
        assert response.json() == {
            "data": {
                "view_on_repos": {
                    "nodes": [{"id": 2, "full_name": "cleopaws/dogspotter"}]
                }
            }
        }
    _schema_cache.clear()


@pytest.mark.asyncio
async def test_time_limit_ms(db_path):
    ds = Datasette(
        [db_path], metadata={"plugins": {"datasette-graphql": {"time_limit_ms": 1}}}
    )
    query = """
    {
        repos(search: "dogspotter") {
            nodes {
                id
                full_name
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 500
        response_json = response.json()
        assert response_json["data"] == {"repos": None}
        assert len(response_json["errors"]) == 1
        assert response_json["errors"][0]["message"].startswith("Time limit exceeded: ")
        assert response_json["errors"][0]["message"].endswith(
            " > 1ms - /test/repos.json?_size=10&_search=dogspotter"
        )


@pytest.mark.asyncio
async def test_num_queries_limit(db_path):
    ds = Datasette(
        [db_path], metadata={"plugins": {"datasette-graphql": {"num_queries_limit": 2}}}
    )
    query = """
    {
        users {
            nodes {
                id
                name
                repos_list {
                    nodes {
                        full_name
                    }
                }
            }
        }
    }
    """
    async with httpx.AsyncClient(app=ds.app()) as client:
        response = await client.post("http://localhost/graphql", json={"query": query})
        assert response.status_code == 500
        assert response.json() == {
            "data": {
                "users": {
                    "nodes": [
                        {
                            "id": 1,
                            "name": "cleopaws",
                            "repos_list": {
                                "nodes": [{"full_name": "cleopaws/dogspotter"}]
                            },
                        },
                        {"id": 2, "name": "simonw", "repos_list": None},
                    ]
                }
            },
            "errors": [
                {
                    "message": "Query limit exceeded: 3 > 2 - /test/repos.json?_size=10&owner=2",
                    "locations": [{"line": 7, "column": 17}],
                    "path": ["users", "nodes", 1, "repos_list"],
                }
            ],
        }
