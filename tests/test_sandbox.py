import tempfile
from rlm_tools_bsl.sandbox import Sandbox


def test_execute_simple_code():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=10_000)
        result = sandbox.execute("x = 2 + 2\nprint(x)")
        assert result.stdout.strip() == "4"
        assert result.error is None


def test_variables_persist_between_executions():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=10_000)
        sandbox.execute("my_var = 42")
        result = sandbox.execute("print(my_var)")
        assert result.stdout.strip() == "42"


def test_output_truncated():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=50)
        result = sandbox.execute("print('a' * 200)")
        assert len(result.stdout) <= 80  # 50 + truncation message


def test_blocked_imports():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=10_000)
        result = sandbox.execute("import subprocess")
        assert result.error is not None


def test_no_write_access():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=10_000)
        result = sandbox.execute(f"open('{tmpdir}/evil.txt', 'w').write('hack')")
        assert result.error is not None


def test_list_variables():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox = Sandbox(base_path=tmpdir, max_output_chars=10_000)
        sandbox.execute("foo = 1\nbar = 'hello'")
        variables = sandbox.list_variables()
        assert "foo" in variables
        assert "bar" in variables


def test_blocks_subclasses_gadget():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("x=().__class__.__bases__[0].__subclasses__()").error is not None


def test_blocks_vars_subscript_bypass():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("x=vars(type)['__subclasses__'](object)").error is not None


def test_blocks_generator_frame_bypass():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("f=(i for i in []).gi_frame").error is not None


def test_blocks_traceback_frame_bypass():
    with tempfile.TemporaryDirectory() as d:
        code = "try:\n 1/0\nexcept Exception as e:\n tb=e.__traceback__"
        assert Sandbox(base_path=d).execute(code).error is not None


def test_blocks_operator_attrgetter_bypass():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("import operator").error is not None


def test_blocks_attribute_assignment():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("import math\nmath.pi = 4").error is not None


def test_blocks_license_printer_file_read_outside_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-OUTSIDE-BASE")
    code = "license._Printer__filenames = [r'%s']\nlicense()" % str(secret)
    r = Sandbox(base_path=str(base)).execute(code)
    assert r.error is not None
    assert "TOPSECRET" not in r.stdout


def test_blocks_dynamic_getattr_dunder():
    with tempfile.TemporaryDirectory() as d:
        assert Sandbox(base_path=d).execute("x=getattr((), '__cl'+'ass__')").error is not None


def test_allows_safe_dunder_name():
    with tempfile.TemporaryDirectory() as d:
        r = Sandbox(base_path=d).execute("print(type([]).__name__)")
        assert r.error is None and "list" in r.stdout


def test_legit_code_still_runs():
    with tempfile.TemporaryDirectory() as d:
        r = Sandbox(base_path=d).execute("d={'a':1}\nprint([k.upper() for k in d])\nprint(getattr(d,'get')('a'))")
        assert r.error is None and "A" in r.stdout and "1" in r.stdout
