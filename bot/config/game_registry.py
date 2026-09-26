"""Single registry for supported Staff games and their configuration keys."""
from dataclasses import dataclass


@dataclass(frozen=True)
class GameDefinition:
    key: str
    label: str
    emoji: str
    color: int
    xp_key: str
    panel_channel_key: str
    results_channel_key: str


GAMES: dict[str, GameDefinition] = {
    "spy": GameDefinition("spy", "Spy", "🎭", 0x5865F2, "game_spy_xp", "spy_panel_channel", "spy_results_channel"),
    "criminal": GameDefinition("criminal", "Criminal", "🕵️", 0x2C3E50, "game_criminal_xp", "criminal_panel_channel", "criminal_results_channel"),
    "mafia": GameDefinition("mafia", "Mafia", "🔪", 0x992D22, "game_mafia_xp", "mafia_panel_channel", "mafia_results_channel"),
    "codenames": GameDefinition("codenames", "Codenames", "🧠", 0x3498DB, "game_codenames_xp", "codenames_panel_channel", "codenames_results_channel"),
    "loup_garou": GameDefinition("loup_garou", "Loup Garou", "🐺", 0x566573, "game_loup_garou_xp", "loup_garou_panel_channel", "loup_garou_results_channel"),
}
