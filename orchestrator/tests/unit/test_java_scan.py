from sdlc_orchestrator.policy.java_scan import contract_endpoints, extract_endpoints, normalize_path

CTRL = """
package x;
@RestController
@RequestMapping("/api")
public class A {
  @PostMapping("/shorten") public void a() {}
  @GetMapping(value = "/analytics/{code}") public void b() {}
  @DeleteMapping(path = {"/links/{id:[0-9]+}", "/l/{id}"}) public void c() {}
  @RequestMapping(value = "/legacy", method = RequestMethod.PUT) public void d() {}
}
"""
ROOT = "@RestController public class R { @GetMapping(\"/{shortCode}\") public void r() {} @GetMapping public void idx() {} }"


def test_normalize():
    assert normalize_path("/api/analytics/{shortCode}/") == "/api/analytics/{}"
    assert normalize_path("api//x/{id:[0-9]+}") == "/api/x/{}"
    assert normalize_path("") == "/"


def test_extracts_class_prefix_and_all_forms():
    eps = extract_endpoints({"A.java": CTRL})
    assert eps == {
        ("POST", "/api/shorten"), ("GET", "/api/analytics/{}"),
        ("DELETE", "/api/links/{}"), ("DELETE", "/api/l/{}"), ("PUT", "/api/legacy"),
    }


def test_root_level_mappings():
    assert extract_endpoints({"R.java": ROOT}) == {("GET", "/{}"), ("GET", "/")}


def test_ignores_tests_and_non_controllers():
    assert extract_endpoints({"src/test/java/T.java": CTRL}) == set()
    assert extract_endpoints({"S.java": "@Service class S { @GetMapping(\"/x\") void a(){} }"}) == set()


def test_contract_endpoints():
    spec = {"paths": {"/api/shorten": {"post": {}, "parameters": []}, "/{shortCode}": {"get": {}}}}
    assert contract_endpoints(spec) == {("POST", "/api/shorten"), ("GET", "/{}")}
