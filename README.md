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

Prawy klik na sygnalizatorze: przejście do czekającego agenta, lista sesji (kliknięcie przełącza do terminala),
tryb warstwy, **Nie przeszkadzać**, **Ustawienia…**, Zamknij.
Lewy przycisk: przeciąganie. Kliknięcie w kropkę sesji przełącza do jej terminala, a kliknięcie w kapsułę –
do najdłużej czekającego agenta. W trybie „pod oknami” kliknięcie chowa też widget pod okna.
Ikona w zasobniku systemowym ma to samo menu; kliknięcie jej wyciąga widget na wierzch.

## Co jeszcze robi widget

- **Przejście do terminala** – hook zapisuje PID procesu agenta (`claude.exe`), a widget idzie w górę drzewa
  procesów aż do okna terminala (Warp, Windows Terminal, VS Code…). Skrót globalny domyślnie `Ctrl+Alt+L`.
  W terminalach z kartami przełącza do okna, nie do konkretnej karty.
- **Przerwanie (Esc)** – co ~2 s widget sprawdza koniec zapisu rozmowy (`transcript_path`); wpis
  „Request interrupted by user” od razu przestawia sesję na bezczynną.
- **Zamknięty terminal** – sesja, której proces agenta już nie żyje, znika od razu (PID + czas utworzenia procesu).
- **Liczniki** – w podpowiedzi i menu: jak długo sesja pracuje / czeka / jest bezczynna.
- **Dźwięki** – gdy ktoś czeka, gdy agent skończy zadanie dłuższe niż próg (domyślnie 3 min) i gdy sesja
  jest bezczynna od N minut (domyślnie 10). Widget celowo nie pokazuje dymków ani powiadomień Windows.
- **Nie przeszkadzać** – ręcznie z menu, automatycznie przy aplikacji pełnoekranowej albo w ustawionych godzinach.
  Kolor nadal się zmienia, ale bez wyskakiwania i dźwięków.

## Mapowanie zdarzeń

Narzędzia pomocniczych agentów (subagenci, agent uruchamiany przez hook `Stop`) mają w danych `agent_id`
i nie zmieniają koloru. Diagnostyka: utwórz pusty plik `%LOCALAPPDATA%\ai-traffic-light\debug`,
a hook zacznie dopisywać surowe zdarzenia do `debug.log` w tym samym folderze.

| Zdarzenie | Stan |
|---|---|
| `SessionStart`, `Stop` | bezczynny |
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | pracuje |
| `Notification` (`permission_prompt`), `PreToolUse` dla `AskUserQuestion` / `ExitPlanMode` | czeka |
| `Stop`, gdy ostatni akapit odpowiedzi zawiera pytanie („Mam to zrobić?”) | czeka (opcja w Ustawienia → Zachowanie) |
| `Notification` (`idle_prompt` i inne informacyjne) | bez zmiany |
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
  czas do uznania sesji za zawieszoną, skrót klawiszowy, kliknięcie przełącza do terminala, Nie przeszkadzać.
- **Dźwięki** – dźwięk przy czekaniu, koniec długiego zadania (próg + dźwięk), przypomnienie o bezczynności
  (czas + dźwięk). Dźwięki z `C:\Windows\Media` albo własny plik `.wav`, przycisk ▶ odtwarza wybrany.
- **Wygląd** (podgląd na żywo) – gotowy styl, obudowa (ciemna / jasna / szklana / automatyczna według jasności tła),
  obwódka i poświata w kolorze stanu (domyślnie włączona), siła poświaty, krycie tła, rozmiar, układ, kropki sesji.
- **System** – autostart z Windows, instalacja/usunięcie hooków, folder stanu.
