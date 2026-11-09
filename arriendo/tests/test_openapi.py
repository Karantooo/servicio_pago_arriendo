from pathlib import Path

import yaml
from openapi_spec_validator import validate

from app.main import app


OPENAPI_PATH = Path(__file__).parents[1] / "openapi.yaml"
OPENAPI_V2_PATH = Path(__file__).parents[1] / "openapiv2.yaml"
HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
}


def _iter_http_routes(routes):
    for route in routes:
        nested_routes = getattr(route, "routes", None)
        if nested_routes is None:
            original_router = getattr(route, "original_router", None)
            nested_routes = getattr(original_router, "routes", None)
        if nested_routes is not None:
            yield from _iter_http_routes(nested_routes)
        else:
            yield route


def _load_openapi(path=OPENAPI_PATH):
    with path.open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def _documented_routes(specification):
    return {
        (path, method)
        for path, path_item in specification["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }


def _implemented_routes(prefix=None):
    return {
        (route.path, method.lower())
        for route in _iter_http_routes(app.routes)
        if getattr(route, "path", None) is not None
        and route.path not in {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        and (prefix is None or route.path.startswith(prefix))
        for method in (getattr(route, "methods", set()) or set())
        if method.lower() in HTTP_METHODS
    }


def test_openapi_document_is_valid():
    validate(_load_openapi())
    validate(_load_openapi(OPENAPI_V2_PATH))


def test_openapi_routes_match_fastapi_implementation():
    documented_routes = _documented_routes(_load_openapi())
    implemented_routes = _implemented_routes()
    v1_implemented_routes = {
        route for route in implemented_routes if not route[0].startswith("/v2/")
    }

    missing_from_openapi = v1_implemented_routes - documented_routes
    missing_from_implementation = documented_routes - v1_implemented_routes

    assert not missing_from_openapi, (
        "Rutas v1 implementadas que faltan en openapi.yaml: "
        f"{sorted(missing_from_openapi)}"
    )
    assert not missing_from_implementation, (
        "Rutas documentadas en openapi.yaml que no existen en v1: "
        f"{sorted(missing_from_implementation)}"
    )


def test_openapiv2_documents_all_implemented_routes():
    documented_routes = _documented_routes(_load_openapi(OPENAPI_V2_PATH))
    implemented_routes = _implemented_routes()

    assert implemented_routes == documented_routes


def test_v2_endpoints_document_error_responses():
    operations = app.openapi()["paths"]
    expected_responses = {
        ("/v2/rentals", "post"): {"400", "401", "403", "404", "409", "500", "502", "503", "504"},
        ("/v2/rentals", "get"): {"400", "401", "403"},
        ("/v2/rentals/{rentalId}", "get"): {"400", "401", "403", "404"},
        ("/v2/rentals/{rentalId}/saga", "get"): {"400", "401", "403", "404"},
        ("/v2/rentals/{rentalId}/cancel", "post"): {"400", "401", "403", "404", "409", "500", "502", "503", "504"},
        ("/v2/rentals/{rentalId}/return", "post"): {"400", "401", "403", "404", "409", "500", "502", "503", "504"},
    }

    for (path, method), expected in expected_responses.items():
        documented = set(operations[path][method]["responses"])
        assert expected <= documented, f"Faltan respuestas documentadas en {method.upper()} {path}"
