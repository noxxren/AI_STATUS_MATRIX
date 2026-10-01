# AI_STATUS_WIDGET

[English](README.md) | **Polski**

Minimalistyczny widget na pulpit Windows, który pokazuje, co robią agenci AI w terminalach.
Każda sesja (terminal) ma własny kwadratowy ekran w stylu Matrix; siatka rośnie jak tekst w książce:
1, 2×1, 2×2, 3×2… – nowy terminal dostaje kolejne wolne miejsce.

| Ekran | Znaczenie |
|---|---|
| biało-turkusowy kursor `>_` | agent bezczynny |
| zielony deszcz znaków | agent pracuje |
| pomarańczowy, pulsujący `?` | agent czeka na Twoją odpowiedź |
| niebieski skaner jak KITT z „Knight Ridera” | agent kompaktuje kontekst rozmowy (`/compact` albo automatycznie) |
| magentowy glitch `ERR` | tura skończyła się błędem API albo sesja „pracuje” bez żadnych zdarzeń (zawieszona) |
| szary szum | brak jakiejkolwiek sesji – agent nie działa albo hooki nie są zainstalowane |

Kliknięcie w kafelek przełącza do terminala tej sesji, a najechanie pokazuje jej szczegóły.
Przy krawędzi ekranu widget rośnie w stronę środka.

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
git clone https://github.com/noxxren/AI_STATUS_WIDGET.git
cd AI_STATUS_WIDGET
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Uruchomienie

1. Hooki: `python install_hooks.py` (albo w widgecie: prawy klik → Ustawienia… → Hooki Claude Code → Zainstaluj).
   Usunięcie: `python install_hooks.py --uninstall`. Podgląd bez zapisu: `--dry-run`.
2. Widget: `start.bat`.
3. Hooki łapią się w **nowo uruchomionych** sesjach Claude Code.

Prawy klik na widgecie: przejście do czekającego agenta, lista sesji (kliknięcie przełącza do terminala),
tryb warstwy, **Nie przeszkadzać**, **Ustawienia…**, Zamknij.
Lewy przycisk: przeciąganie. Kliknięcie w kafelek przełącza do terminala tej sesji, a kliknięcie obok kafelków –
do najdłużej czekającego agenta. W trybie „pod oknami” kliknięcie chowa też widget pod okna.
Ikona w zasobniku systemowym ma to samo menu; kliknięcie jej wyciąga widget na wierzch.

## Co jeszcze robi widget

- **Przejście do terminala** – hook zapisuje PID procesu agenta (`claude.exe`), a widget idzie w górę drzewa
  procesów aż do okna terminala (Warp, Windows Terminal, VS Code…). Opcjonalny skrót globalny (domyślnie wyłączony,
  ustawisz go w Ustawienia → Zachowanie → Skrót do agenta, np. `Ctrl+Alt+L`).
  W terminalach z kartami przełącza do okna, nie do konkretnej karty.
- **Przerwanie (Esc)** – co ~2 s widget sprawdza koniec zapisu rozmowy (`transcript_path`); wpis
  „Request interrupted by user” od razu przestawia sesję na bezczynną.
- **Błąd tury** – wpis z `isApiErrorMessage` na końcu zapisu rozmowy (np. „API Error: 500”, zerwane połączenie)
  przestawia sesję na błąd. Sprawdza to hook przy `Stop` i widget co ~2 s, bo po błędzie `Stop` nie zawsze przychodzi.
  Kolejne polecenie wysłane do agenta kasuje błąd.
- **Zamknięty terminal** – sesja, której proces agenta już nie żyje, znika od razu (PID + czas utworzenia procesu).
- **Liczniki** – w podpowiedzi i menu: jak długo sesja pracuje / czeka / jest bezczynna.
- **Dźwięki** – gdy ktoś czeka, gdy agent skończy zadanie dłuższe niż próg (domyślnie 3 min) i gdy sesja
  jest bezczynna od N minut (domyślnie 10). Widget celowo nie pokazuje dymków ani powiadomień Windows.
- **Nie przeszkadzać** – ręcznie z menu, automatycznie przy aplikacji pełnoekranowej albo w ustawionych godzinach.
  Ekrany nadal się zmieniają, ale bez wyskakiwania i dźwięków.

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
| `Stop` (albo jego brak), gdy tura skończyła się błędem API | błąd |
| `PreCompact` | kompaktuje; po nim `SessionStart` (`source: compact`) wraca do pracy (auto) albo bezczynności (`/compact`) |
| `Notification` (`idle_prompt` i inne informacyjne) | bez zmiany |
| `SessionEnd` | sesja znika |

## Inne CLI

`hook.py` rozumie też zdarzenia Gemini CLI (`BeforeAgent`, `BeforeTool`, `AfterAgent`…) i Codex CLI.
Konfiguracji nie instalujemy automatycznie – przykłady:

- **Gemini CLI** (`~/.gemini/settings.json`, sekcja `hooks`): komenda
  `python -S D:/tools/ai-traffic-light/hook.py --cli gemini` dla `SessionStart`, `BeforeAgent`,
  `BeforeTool`, `AfterAgent`, `PreCompress`, `Notification`, `SessionEnd`.
- **Codex CLI** (`~/.codex/config.toml`):
  `notify = ["python", "-S", "D:/tools/ai-traffic-light/hook.py", "--cli", "codex"]`.
  Codex zgłasza tylko koniec tury, więc pokaże bezczynność po skończeniu pracy, ale nie samą pracę.

## Ustawienia

Zapisywane w `%LOCALAPPDATA%\ai-traffic-light\config.json`, podzielone na zakładki:

- **Zachowanie** – warstwa (zawsze na wierzchu / pod oknami), kiedy wyskakiwać, jak długo zostać na wierzchu,
  czas do uznania sesji za zawieszoną, skrót klawiszowy, kliknięcie przełącza do terminala, Nie przeszkadzać.
- **Dźwięki** – dźwięk przy czekaniu, koniec długiego zadania (próg + dźwięk), przypomnienie o bezczynności
  (czas + dźwięk). Dźwięki z `C:\Windows\Media` albo własny plik `.wav`, przycisk ▶ odtwarza wybrany.
- **Wygląd** (podgląd na żywo) – gotowy styl, obudowa kafelków (ciemna / jasna / szklana / automatyczna według
  jasności tła), obwódka i poświata w kolorze stanu (domyślnie włączona), siła poświaty, krycie tła, rozmiar.
- **System** – język (automatycznie według Windows / polski / angielski), autostart z Windows,
  instalacja/usunięcie hooków, folder stanu.
