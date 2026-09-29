# Sygnalizator AI

Minimalistyczny widget na pulpit Windows, który pokazuje, co robią agenci AI w terminalach.

| Światło | Znaczenie |
|---|---|
| zielone | wszyscy agenci bezczynni |
| czerwone | przynajmniej jeden pracuje |
| pomarańczowe (pulsuje) | ktoś czeka na Twoją odpowiedź; ma pierwszeństwo przed czerwonym |

Małe kropki pod sygnalizatorem to poszczególne sesje. Szara kropka oznacza sesję bez sygnału
(np. przerwaną klawiszem Esc albo zamkniętym terminalem).

## Jak to działa

```
Claude Code ──hook──▶ hook.py ──zapis──▶ %LOCALAPPDATA%\ai-traffic-light\sessions\<session_id>.json
                                                              │
                                   widget.pyw ◀──odczyt co 300 ms
```

- `hook.py` – tylko biblioteka standardowa, zawsze kończy się kodem 0 i nic nie wypisuje, więc nie blokuje agenta.
- `widget.pyw` – PySide6 (venv w `.venv`). Warstwę okna ustawia przez `SetWindowPos` z `SWP_NOACTIVATE`,
  więc wyskakując na wierzch nie zabiera fokusu terminalowi.
- `install_hooks.py` – dopisuje/usuwa nasze hooki w `~/.claude/settings.json` (z kopią zapasową).

## Instalacja

Wymagania: Windows 10/11, Python 3.10+ (`python` w PATH).

```bat
git clone https://github.com/noxxren/ai_traffic_light.git
cd ai_traffic_light
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Uruchomienie

1. Hooki: `python install_hooks.py` (albo w widgecie: prawy klik → Ustawienia… → Hooki Claude Code → Zainstaluj).
   Usunięcie: `python install_hooks.py --uninstall`. Podgląd bez zapisu: `--dry-run`.
2. Widget: `start.bat`.
3. Hooki łapią się w **nowo uruchomionych** sesjach Claude Code.

Prawy klik na sygnalizatorze: lista sesji, tryb warstwy, **Ustawienia…**, Zamknij.
Lewy przycisk: przeciąganie. Kliknięcie w trybie „pod oknami” chowa widget z powrotem pod okna.
Ikona w zasobniku systemowym ma to samo menu; kliknięcie jej wyciąga widget na wierzch.

## Mapowanie zdarzeń

| Zdarzenie | Stan |
|---|---|
| `SessionStart`, `Stop` | bezczynny |
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | pracuje |
| `Notification` (`permission_prompt`), `PreToolUse` dla `AskUserQuestion` / `ExitPlanMode` | czeka |
| `Notification` (`idle_prompt`) | bezczynny |
| `SessionEnd` | sesja znika |

## Inne CLI

`hook.py` rozumie też zdarzenia Gemini CLI (`BeforeAgent`, `BeforeTool`, `AfterAgent`…) i Codex CLI.
Konfiguracji nie instalujemy automatycznie – przykłady:

- **Gemini CLI** (`~/.gemini/settings.json`, sekcja `hooks`): komenda
  `python -S D:/tools/ai-traffic-light/hook.py --cli gemini` dla `SessionStart`, `BeforeAgent`,
  `BeforeTool`, `AfterAgent`, `Notification`, `SessionEnd`.
- **Codex CLI** (`~/.codex/config.toml`):
  `notify = ["python", "-S", "D:/tools/ai-traffic-light/hook.py", "--cli", "codex"]`.
  Codex zgłasza tylko koniec tury, więc pokaże zielone po skończeniu pracy, ale nie czerwone na jej początku.

## Ustawienia

Zapisywane w `%LOCALAPPDATA%\ai-traffic-light\config.json`, podzielone na zakładki:

- **Zachowanie** – warstwa (zawsze na wierzchu / pod oknami), kiedy wyskakiwać, jak długo zostać na wierzchu,
  czas do uznania sesji za zawieszoną, dźwięk przy pomarańczowym.
- **Wygląd** (podgląd na żywo) – gotowy styl, obudowa (ciemna / jasna / szklana / automatyczna według jasności tła),
  obwódka i poświata w kolorze stanu (domyślnie włączona), siła poświaty, krycie tła, rozmiar, układ, kropki sesji.
- **System** – autostart z Windows, instalacja/usunięcie hooków, folder stanu.
