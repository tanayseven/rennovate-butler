import json
from types import SimpleNamespace

import pytest

from renovate_butler.fixer import Usage, Workspace, checks_from_workflows, run_agent


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\ny = 2\n")
    (tmp_path / ".git").mkdir()
    return Workspace(tmp_path, checks=["echo ok", "exit 3"])


def test_path_escape_rejected(ws):
    for bad in ("../outside.txt", "/etc/passwd", "src/../../x"):
        with pytest.raises(ValueError):
            ws.read_file(bad)
    with pytest.raises(ValueError):
        ws.write_file(".git/config", "pwned")


def test_read_grep_list(ws):
    assert "1: x = 1" in ws.read_file("src/app.py")
    assert ws.grep(r"y =") == "src/app.py:2: y = 2"
    assert ".git" not in ws.list_dir(".")


def test_apply_edit(ws):
    ws.apply_edit("src/app.py", "y = 2", "y = 3")
    assert "y = 3" in ws.read_file("src/app.py")
    with pytest.raises(ValueError, match="exactly once"):
        ws.apply_edit("src/app.py", "nope", "z")


def test_run_check_allowlist(ws):
    assert ws.run_check(0).startswith("exit code 0")
    assert ws.run_check(1).startswith("exit code 3")
    with pytest.raises(ValueError):
        ws.run_check(5)


def test_checks_from_workflows_skips_secret_steps():
    wf = """
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - run: uv run pytest
      - run: deploy --token ${{ secrets.TOKEN }}
"""
    assert checks_from_workflows([wf, "::bad yaml::["]) == ["uv run pytest"]


def _call(i, name, **args):
    return SimpleNamespace(
        id=f"c{i}",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        model_dump=lambda: {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        },
    )


class ScriptedClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(kw)
        calls = self.turns.pop(0)
        msg = SimpleNamespace(content="", tool_calls=calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_agent_loop_edits_and_finishes(ws):
    client = ScriptedClient(
        [
            [_call(1, "read_file", path="src/app.py")],
            [_call(2, "apply_edit", path="src/app.py", old="y = 2", new="y = 5"), _call(3, "read_file", path="../x")],
            [_call(4, "finish", summary="bump y", explanation="because")],
        ]
    )
    summary, expl = run_agent(client, "m", ws, "ctx", max_turns=10, log=lambda *_: None)
    assert (summary, expl) == ("bump y", "because")
    assert "y = 5" in ws.read_file("src/app.py")
    tool_msgs = [m for m in client.seen[-1]["messages"] if m["role"] == "tool"]
    assert any("escapes" in m["content"] for m in tool_msgs)
    # run_check is hidden unless explicitly allowed
    assert "run_check" not in {t["function"]["name"] for t in client.seen[0]["tools"]}


def test_agent_turn_limit(ws):
    client = ScriptedClient([[_call(i, "list_dir")] for i in range(3)])
    summary, _ = run_agent(client, "m", ws, "ctx", max_turns=3, log=lambda *_: None)
    assert "turn limit" in summary


def test_usage_adds_up_openrouter_responses():
    u = Usage()
    u.add_response(SimpleNamespace(prompt_tokens=1000, completion_tokens=200, cost=0.0042))
    u.add_response(SimpleNamespace(prompt_tokens=1500, completion_tokens=None))  # no cost reported
    u.add_response(None)  # no usage at all still counts the call
    assert (u.calls, u.input_tokens, u.output_tokens, round(u.cost_usd, 6)) == (3, 2500, 200, 0.0042)
    assert u.fields() == {"llm_calls": 3, "input_tokens": 2500, "output_tokens": 200, "cost_usd": 0.0042}
    assert (u + Usage(1, 10, 5, 0.01)).fields()["cost_usd"] == 0.0142


def test_run_agent_records_usage_and_asks_openrouter_for_cost(tmp_path):
    seen = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            seen.append(kw)
            msg = SimpleNamespace(content="", tool_calls=[_call(1, "finish", summary="s", explanation="e")])
            usage = SimpleNamespace(prompt_tokens=800, completion_tokens=50, cost=0.003)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    u = Usage()
    run_agent(Client(), "m", Workspace(tmp_path), "ctx", max_turns=3, log=lambda *_: None, usage=u)
    assert seen[0]["extra_body"] == {"usage": {"include": True}}
    assert (u.calls, u.input_tokens, u.output_tokens, u.cost_usd) == (1, 800, 50, 0.003)
