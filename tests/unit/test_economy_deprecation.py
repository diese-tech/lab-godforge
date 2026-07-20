import bot
from utils import ledger as ledger_utils
from utils import wallet as wallet_utils


async def test_economy_commands_are_deprecated_without_mutating_state(
    tmp_ledger, tmp_wallets, mock_message
):
    commands = [
        ".match create @TeamA @TeamB",
        ".bet GF-0001 50 @TeamA win",
        ".wallet give @Player 100",
        ".ledger reset",
    ]

    for command in commands:
        mock_message.content = command
        mock_message.channel.send.reset_mock()

        await bot.on_message(mock_message)

        mock_message.channel.send.assert_called_once()
        reply = mock_message.channel.send.call_args[0][0].lower()
        assert "deprecated" in reply
        assert "standalone godforge" in reply
        assert "forgelens" not in reply

    assert ledger_utils.load_ledger()["matches"] == []
    assert wallet_utils.load_wallets() == {}
