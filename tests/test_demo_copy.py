"""The offline demo prints the approved receipt replay wording."""

from argparse import Namespace

from demo import run


async def test_offline_demo_prints_retry_wording(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CHECKOUT_PRICE_USD", "25.00")

    summary = await run(Namespace(mock_paypal=True, mock_llm=True, out=str(tmp_path)))

    output = capsys.readouterr().out
    assert "a retry with the same key returns the original receipt instead of dispatching again." in output
    assert "scripted model" in output
    assert summary["retry_captures"] == 1
    assert summary["retry_debits"] == 1
