from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or "assetto-launcher-dev-key"

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DEFAULT_RACE_INI = BASE_DIR / "templates" / "race.ini"
CONFIG_FILE = INSTANCE_DIR / "launcher_config.json"
STATE_FILE = INSTANCE_DIR / "launcher_state.json"
COMMAND_LOG_FILE = INSTANCE_DIR / "command_log.jsonl"
ACTIVE_GAME_FILE = INSTANCE_DIR / "active_game.json"

AVAILABLE_ACTIONS = {
    "shift_up": "Hochschalten",
    "shift_down": "Runterschalten",
}

CONTROL_ACTION_KEYS = {
    "shift_up": "X",
    "shift_down": "Y",
}


@dataclass(frozen=True)
class LauncherSettings:
    race_ini_path: Path
    content_root: Path | None
    tracks_root: Path
    cars_root: Path
    game_executable: str | None
    game_args: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file_handle:
        payload = json.load(file_handle)
    return payload if isinstance(payload, dict) else {}


def load_settings() -> LauncherSettings:
    file_config = read_json_file(CONFIG_FILE)

    race_ini_value = os.environ.get("RACE_INI_PATH") or file_config.get("race_ini_path")
    race_ini_path = Path(race_ini_value) if race_ini_value else DEFAULT_RACE_INI

    content_root_value = os.environ.get("ASSETTO_CONTENT_ROOT") or file_config.get("content_root")
    content_root = Path(content_root_value) if content_root_value else None

    tracks_root_value = os.environ.get("ASSETTO_TRACKS_ROOT") or file_config.get("tracks_root")
    tracks_root = Path(tracks_root_value) if tracks_root_value else (content_root or BASE_DIR.parent.parent.parent / "GameFiles" / "tracks")

    cars_root_value = os.environ.get("ASSETTO_CARS_ROOT") or file_config.get("cars_root")
    cars_root = Path(cars_root_value) if cars_root_value else BASE_DIR.parent.parent.parent / "GameFiles" / "cars"

    game_executable = os.environ.get("ASSETTO_EXECUTABLE") or file_config.get("game_executable")

    launch_args_value = os.environ.get("ASSETTO_LAUNCH_ARGS") or file_config.get("game_args") or []
    if isinstance(launch_args_value, str):
        game_args = shlex.split(launch_args_value)
    elif isinstance(launch_args_value, Iterable):
        game_args = [str(item) for item in launch_args_value]
    else:
        game_args = []

    return LauncherSettings(
        race_ini_path=race_ini_path,
        content_root=content_root,
        tracks_root=tracks_root,
        cars_root=cars_root,
        game_executable=game_executable,
        game_args=game_args,
    )


def save_launcher_config_from_form(form_data: dict) -> None:
    config = read_json_file(CONFIG_FILE)
    key_map = {
        "race_ini_path": "race_ini_path",
        "tracks_root": "tracks_root",
        "cars_root": "cars_root",
        "game_executable": "game_executable",
        "game_args": "game_args",
    }

    for form_key, config_key in key_map.items():
        value = (form_data.get(form_key) or "").strip()
        if value:
            config[config_key] = value
        else:
            config.pop(config_key, None)

    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=True), encoding="utf-8")


def save_active_game_process(pid: int) -> None:
    ACTIVE_GAME_FILE.write_text(
        json.dumps({"pid": pid, "started_at": utc_now()}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def read_active_game_process() -> dict:
    if not ACTIVE_GAME_FILE.exists():
        return {}
    try:
        payload = json.loads(ACTIVE_GAME_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_active_game_process() -> None:
    if ACTIVE_GAME_FILE.exists():
        ACTIVE_GAME_FILE.unlink()


def pm2_game_is_available() -> bool:
    try:
        result = os.system(
            ["pm2", "describe", "Game"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False

    return result.returncode == 0


def settings_template_context(
    message: str = "",
    message_kind: str = "info",
    settings_locked: bool = False,
    settings_password_value: str = "",
) -> dict:
    settings = load_settings()
    return {
        "message": message,
        "message_kind": message_kind,
        "settings_locked": settings_locked,
        "settings_password_value": settings_password_value,
        "settings_values": {
            "race_ini_path": str(settings.race_ini_path),
            "tracks_root": str(settings.tracks_root),
            "cars_root": str(settings.cars_root),
            "game_executable": settings.game_executable or "",
            "game_args": " ".join(settings.game_args),
        },
    }


def settings_access_password() -> str:
    return (os.environ.get("ASSETTO_SETTINGS_PASSWORD") or "assetto").strip()


def open_system_dialog(kind: str, initial_path: str = "") -> tuple[bool, str]:
    kind = (kind or "").strip().lower()
    if kind not in {"folder", "file"}:
        return False, "Nicht unterstützter Dialogtyp."

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on OS GUI availability
        return False, f"Dialog-Backend ist nicht verfügbar: {exc}"

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    options: dict = {}
    if initial_path:
        initial = Path(initial_path)
        if kind == "folder":
            if initial.exists() and initial.is_dir():
                options["initialdir"] = str(initial)
        else:
            if initial.exists() and initial.is_file():
                options["initialdir"] = str(initial.parent)
                options["initialfile"] = initial.name
            elif initial.exists() and initial.is_dir():
                options["initialdir"] = str(initial)

    try:
        if kind == "folder":
            selected = filedialog.askdirectory(**options)
        else:
            selected = filedialog.askopenfilename(**options)
    except Exception as exc:  # pragma: no cover - depends on OS GUI availability
        root.destroy()
        return False, f"Systemdialog konnte nicht geöffnet werden: {exc}"

    root.destroy()

    if not selected:
        return False, "Keine Auswahl getroffen."

    return True, selected


def send_server_keypress(key_char: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Tastatursteuerung ist nur unter Windows verfügbar."

    key_upper = (key_char or "").strip().upper()
    virtual_keys = {
        "X": 0x58,
        "Y": 0x59,
    }
    virtual_key = virtual_keys.get(key_upper)
    if virtual_key is None:
        return False, "Nicht unterstützte Taste."

    try:
        import ctypes

        key_event = ctypes.windll.user32.keybd_event
        key_event(virtual_key, 0, 0, 0)
        time.sleep(0.015)
        key_event(virtual_key, 0, 0x0002, 0)
    except Exception as exc:
        return False, f"Tasteneingabe fehlgeschlagen: {exc}"

    return True, ""


def read_race_ini(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_track_metadata(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def section_value(metadata: dict, key: str, fallback: str = "") -> str:
    value = metadata.get(key, fallback)
    if value is None:
        return fallback
    return str(value)


def asset_url(relative_path: str) -> str:
    return url_for("track_asset", relative_path=relative_path)


def track_asset_url(track_dir: Path, file_path: Path) -> str:
    relative_path = Path(track_dir.name) / file_path.relative_to(track_dir)
    return asset_url(str(relative_path).replace("\\", "/"))


def car_asset_url(car_dir: Path, file_path: Path) -> str:
    relative_path = Path(car_dir.name) / file_path.relative_to(car_dir)
    return url_for("car_asset", relative_path=str(relative_path).replace("\\", "/"))


def find_car_preview_file(car_dir: Path) -> Path | None:
    skins_dir = car_dir / "skins"
    if not skins_dir.exists() or not skins_dir.is_dir():
        return None

    skin_dirs = sorted((entry for entry in skins_dir.iterdir() if entry.is_dir()), key=lambda path: path.name.lower())
    for skin_dir in skin_dirs:
        preview_jpg = skin_dir / "preview.jpg"
        if preview_jpg.exists():
            return preview_jpg
        preview_png = skin_dir / "preview.png"
        if preview_png.exists():
            return preview_png

    return None


def build_layout_entry(track_dir: Path, layout_dir: Path | None) -> dict:
    if layout_dir is None:
        json_path = track_dir / "ui" / "ui_track.json"
        preview_path = track_dir / "ui" / "preview.png"
        map_candidates = [track_dir / "map.png", track_dir / "ui" / "outline.png"]
        layout_id = "default"
        config_track = ""
        is_default_layout = True
    else:
        json_path = layout_dir / "ui_track.json"
        preview_path = layout_dir / "preview.png"
        map_candidates = [track_dir / layout_dir.name / "map.png", layout_dir / "outline.png"]
        layout_id = layout_dir.name
        config_track = layout_dir.name
        is_default_layout = False

    metadata = read_track_metadata(json_path)

    preview_url = track_asset_url(track_dir, preview_path) if preview_path.exists() else ""
    map_url = ""
    for candidate in map_candidates:
        if candidate.exists():
            map_url = track_asset_url(track_dir, candidate)
            break

    layout_name = section_value(metadata, "name", layout_id.replace("_", " ").title())

    return {
        "layout_id": layout_id,
        "config_track": config_track,
        "folder_name": layout_id if layout_dir is not None else track_dir.name,
        "name": layout_name,
        "description": section_value(metadata, "description"),
        "tags": metadata.get("tags", []) if isinstance(metadata.get("tags", []), list) else [],
        "country": section_value(metadata, "country"),
        "city": section_value(metadata, "city"),
        "length": section_value(metadata, "length"),
        "width": section_value(metadata, "width"),
        "pitboxes": section_value(metadata, "pitboxes"),
        "run": section_value(metadata, "run"),
        "author": section_value(metadata, "author"),
        "version": section_value(metadata, "version"),
        "year": metadata.get("year", ""),
        "geotags": metadata.get("geotags", []) if isinstance(metadata.get("geotags", []), list) else [],
        "preview_url": preview_url,
        "map_url": map_url,
        "is_default_layout": is_default_layout,
        "is_multi_layout": layout_dir is not None,
    }


def build_track_entry(track_dir: Path) -> dict | None:
    ui_root = track_dir / "ui"
    if not ui_root.exists() or not ui_root.is_dir():
        return None

    layout_dirs = sorted(
        entry for entry in ui_root.iterdir() if entry.is_dir() and (entry / "ui_track.json").exists()
    )

    if layout_dirs:
        layouts = [build_layout_entry(track_dir, layout_dir) for layout_dir in layout_dirs]
        display_metadata = read_track_metadata(ui_root / "ui_track.json")
        if not display_metadata:
            display_metadata = read_track_metadata(layout_dirs[0] / "ui_track.json")
        preview_url = layouts[0]["preview_url"] if layouts and layouts[0]["preview_url"] else ""
        map_file = track_dir / "map.png"
        map_url = track_asset_url(track_dir, map_file) if map_file.exists() else layouts[0]["map_url"]
    else:
        layouts = [build_layout_entry(track_dir, None)]
        display_metadata = read_track_metadata(ui_root / "ui_track.json")
        preview_url = layouts[0]["preview_url"]
        map_url = layouts[0]["map_url"]

    display_name = section_value(display_metadata, "name", track_dir.name.replace("_", " ").title())

    return {
        "track_id": track_dir.name,
        "folder_name": track_dir.name,
        "name": display_name,
        "description": section_value(display_metadata, "description"),
        "tags": display_metadata.get("tags", []) if isinstance(display_metadata.get("tags", []), list) else [],
        "country": section_value(display_metadata, "country"),
        "city": section_value(display_metadata, "city"),
        "length": section_value(display_metadata, "length"),
        "width": section_value(display_metadata, "width"),
        "pitboxes": section_value(display_metadata, "pitboxes"),
        "run": section_value(display_metadata, "run"),
        "author": section_value(display_metadata, "author"),
        "version": section_value(display_metadata, "version"),
        "year": display_metadata.get("year", ""),
        "geotags": display_metadata.get("geotags", []) if isinstance(display_metadata.get("geotags", []), list) else [],
        "preview_url": preview_url,
        "map_url": map_url,
        "layouts": layouts,
        "layout_count": len(layouts),
        "multi_layout": len(layouts) > 1,
    }


def discover_track_catalog(tracks_root: Path) -> list[dict]:
    if not tracks_root.exists() or not tracks_root.is_dir():
        return []

    tracks: list[dict] = []
    for entry in sorted(tracks_root.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir():
            continue
        track_entry = build_track_entry(entry)
        if track_entry is not None:
            tracks.append(track_entry)
    return tracks


def discover_car_catalog(cars_root: Path) -> list[dict]:
    if not cars_root.exists() or not cars_root.is_dir():
        return []

    cars: list[dict] = []
    for entry in sorted(cars_root.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir():
            continue

        logo_file = entry / "ui" / "badge.png"
        preview_file = find_car_preview_file(entry)

        cars.append(
            {
                "car_id": entry.name,
                "folder_name": entry.name,
                "name": entry.name.replace("_", " "),
                "logo_url": car_asset_url(entry, logo_file) if logo_file.exists() else "",
                "preview_url": car_asset_url(entry, preview_file) if preview_file is not None else "",
            }
        )
    return cars


def read_selection_state(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        return "", "", ""

    current_track = ""
    current_layout = ""
    current_car = ""
    current_section = ""

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip().lower()
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if current_section == "race":
            if key == "track":
                current_track = value
            elif key == "config_track":
                current_layout = value
            elif key == "model":
                current_car = value

    return current_track, current_layout, current_car


def update_track_selection(path: Path, track_id: str, layout_config: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Race-ini-Datei nicht gefunden: {path}")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    update_key_in_section(lines, "RACE", "TRACK", track_id)
    update_key_in_section(lines, "RACE", "CONFIG_TRACK", layout_config)
    path.write_text("".join(lines), encoding="utf-8")


def update_car_selection(path: Path, car_id: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Race-ini-Datei nicht gefunden: {path}")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    update_key_in_section(lines, "RACE", "MODEL", car_id)
    update_key_in_section(lines, "RACE", "REQUESTED_CAR", car_id)
    update_key_in_section(lines, "CAR_0", "MODEL", car_id)
    path.write_text("".join(lines), encoding="utf-8")


def find_track(tracks: list[dict], track_id: str) -> dict | None:
    return next((track for track in tracks if track["track_id"] == track_id), None)


def find_layout(track: dict, layout_id: str) -> dict | None:
    return next((layout for layout in track["layouts"] if layout["layout_id"] == layout_id), None)


def find_car(cars: list[dict], car_id: str) -> dict | None:
    return next((car for car in cars if car["car_id"] == car_id), None)


def has_full_selection(context: dict) -> bool:
    return bool(context.get("selected_track") and context.get("selected_layout") and context.get("selected_car_entry"))


def build_browser_context(message: str | None = None, message_kind: str = "info") -> dict:
    settings = load_settings()
    tracks = discover_track_catalog(settings.tracks_root)
    cars = discover_car_catalog(settings.cars_root)
    current_track, current_layout, current_car = read_selection_state(settings.race_ini_path)
    selected_track = find_track(tracks, current_track)
    selected_layout = find_layout(selected_track, current_layout or "default") if selected_track else None
    selected_car_entry = find_car(cars, current_car)
    active_game_process = read_active_game_process()

    state = {
        "race_ini_path": str(settings.race_ini_path),
        "tracks_root": str(settings.tracks_root),
        "cars_root": str(settings.cars_root),
        "game_executable": settings.game_executable or "",
        "game_args": settings.game_args,
        "track_count": len(tracks),
        "car_count": len(cars),
        "tracks": tracks,
        "cars": cars,
        "selected_track_id": current_track,
        "selected_layout_id": current_layout,
        "selected_car": current_car,
        "selected_car_entry": selected_car_entry,
        "selected_track": selected_track,
        "selected_layout": selected_layout,
        "active_game_pid": active_game_process.get("pid", ""),
        "message": message or "",
        "message_kind": message_kind,
        "recent_commands": read_recent_commands(),
    }
    write_state(state)
    return state


def read_current_selection(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""

    current_track = ""
    current_car = ""
    current_section = ""

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip().lower()
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if current_section == "race" and key == "track":
            current_track = value
        elif current_section == "race" and key in {"model", "requested_car"}:
            current_car = value
        elif current_section == "car_0" and key == "model" and not current_car:
            current_car = value

    return current_track, current_car


def write_state(payload: dict) -> None:
    STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def append_command_log(action: str, details: dict | None = None) -> None:
    record = {
        "timestamp": utc_now(),
        "action": action,
        "details": details or {},
    }
    with COMMAND_LOG_FILE.open("a", encoding="utf-8") as file_handle:
        file_handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def read_recent_commands(limit: int = 12) -> list[dict]:
    if not COMMAND_LOG_FILE.exists():
        return []

    with COMMAND_LOG_FILE.open("r", encoding="utf-8") as file_handle:
        lines = [line.strip() for line in file_handle if line.strip()]

    recent_lines = lines[-limit:]
    recent_commands: list[dict] = []

    for line in recent_lines:
        try:
            recent_commands.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return recent_commands


def list_directory_names(path: Path | None) -> list[str]:
    if path is None or not path.exists() or not path.is_dir():
        return []
    return sorted(entry.name for entry in path.iterdir() if entry.is_dir())


def discover_tracks_and_cars(content_root: Path | None) -> tuple[list[str], list[str]]:
    if content_root is None:
        return [], []

    tracks_root = content_root / "content" / "tracks"
    cars_root = content_root / "content" / "cars"
    return list_directory_names(tracks_root), list_directory_names(cars_root)


def update_key_in_section(lines: list[str], section: str, key: str, value: str) -> None:
    section_name = section.strip().lower()
    key_name = key.strip().lower()
    in_section = False
    section_end = None
    key_updated = False

    for index, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not key_updated:
                section_end = index
                break
            in_section = stripped.lower() == f"[{section_name}]"
            continue

        if not in_section or "=" not in line:
            continue

        current_key = line.split("=", 1)[0].strip().lower()
        if current_key == key_name:
            lines[index] = f"{key}={value}\n"
            key_updated = True

    if in_section and not key_updated:
        insert_index = section_end if section_end is not None else len(lines)
        lines.insert(insert_index, f"{key}={value}\n")


def update_race_ini_values(path: Path, track: str, car: str) -> None:
    """Update the selected track and car in race.ini."""
    if not path.exists():
        raise FileNotFoundError(f"Race-ini-Datei nicht gefunden: {path}")

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    update_key_in_section(lines, "RACE", "TRACK", track)
    update_key_in_section(lines, "RACE", "MODEL", car)
    update_key_in_section(lines, "RACE", "REQUESTED_CAR", car)
    update_key_in_section(lines, "CAR_0", "MODEL", car)
    path.write_text("".join(lines), encoding="utf-8")


def launch_game(settings: LauncherSettings) -> tuple[bool, str]:
    if pm2_game_is_available():
        command = "pm2 start Game"
        launch_message = "Spielstart-Befehl gesendet: pm2 start Game"
    else:
        command = 'cd "C:\\Users\\Fahrsimulator\\Desktop\\AC_PRO 19" && pm2 start acs_pro.exe --name Game'
        launch_message = 'Spielstart-Befehl gesendet: pm2 start C:\\Users\\Fahrsimulator\\Desktop\\AC_PRO 19\\acs_pro.exe'

    try:
        exit_code = os.system(command)
    except Exception as exc:  # pragma: no cover - surfaced to the UI instead
        return False, f"Spielstart fehlgeschlagen: {exc}"

    if exit_code != 0:
        return False, f"Spielstart fehlgeschlagen: {command} (Exit-Code {exit_code})"

    clear_active_game_process()

    return True, launch_message


def load_dashboard_context(message: str | None = None, message_kind: str = "info") -> dict:
    settings = load_settings()
    tracks, cars = discover_tracks_and_cars(settings.content_root)
    race_ini_content = read_race_ini(settings.race_ini_path)
    current_track, current_car = read_current_selection(settings.race_ini_path)
    recent_commands = read_recent_commands()

    state = {
        "race_ini_path": str(settings.race_ini_path),
        "content_root": str(settings.content_root) if settings.content_root else "",
        "game_executable": settings.game_executable or "",
        "game_args": settings.game_args,
        "current_track": current_track,
        "current_car": current_car,
        "tracks": tracks,
        "cars": cars,
        "race_ini_content": race_ini_content,
        "recent_commands": recent_commands,
        "message": message or "",
        "message_kind": message_kind,
    }
    write_state(state)
    return state


def extract_selection(form_data: dict) -> tuple[str, str]:
    track = (form_data.get("track") or form_data.get("TRACK") or "").strip()
    car = (form_data.get("car") or form_data.get("MODEL") or "").strip()
    return track, car


@app.get("/")
def index():
    return redirect(url_for("tracks_overview"))


@app.get("/race")
def race_get():
    return redirect(url_for("tracks_overview"))


@app.get("/tracks")
def tracks_overview():
    context = build_browser_context()
    return render_template("tracks.html", **context)


@app.get("/settings")
def settings_page():
    message = (request.args.get("message") or "").strip()
    message_kind = (request.args.get("kind") or "info").strip() or "info"
    if not message:
        message = "Passwort eingeben, um Einstellungen zu öffnen."
        message_kind = "warning"
    return render_template(
        "settings.html",
        **settings_template_context(message=message, message_kind=message_kind, settings_locked=True),
        back_url=url_for("tracks_overview"),
    )


@app.post("/settings/unlock")
def settings_unlock():
    submitted_password = (request.form.get("settings_password") or "").strip()
    if submitted_password != settings_access_password():
        return redirect(url_for("settings_page", message="Falsches Passwort.", kind="error"))

    return render_template(
        "settings.html",
        **settings_template_context(
            message="Einstellungen freigeschaltet.",
            message_kind="success",
            settings_locked=False,
            settings_password_value=submitted_password,
        ),
        back_url=url_for("tracks_overview"),
    )


@app.post("/settings")
def settings_save():
    submitted_password = (request.form.get("settings_password") or "").strip()
    if submitted_password != settings_access_password():
        return redirect(url_for("settings_page", message="Kein Zugriff auf Einstellungen.", kind="error"))
    save_launcher_config_from_form(request.form)
    return redirect(url_for("tracks_overview"))


@app.post("/api/dialog")
def api_dialog():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind", "")).strip().lower()
    initial_path = str(payload.get("initialPath", "")).strip()

    ok, result = open_system_dialog(kind=kind, initial_path=initial_path)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400

    return jsonify({"ok": True, "path": result})


@app.get("/tracks/<track_id>")
def track_detail(track_id: str):
    context = build_browser_context()
    track = find_track(context["tracks"], track_id)
    if track is None:
        abort(404, description="Strecke nicht gefunden.")
    return render_template("track_detail.html", **context, track=track, back_url=url_for("tracks_overview"))


@app.post("/tracks/<track_id>/layouts/<layout_id>/select")
def select_layout(track_id: str, layout_id: str):
    context = build_browser_context()
    track = find_track(context["tracks"], track_id)
    if track is None:
        abort(404, description="Strecke nicht gefunden.")

    layout = find_layout(track, layout_id)
    if layout is None:
        abort(404, description="Layout nicht gefunden.")

    settings = load_settings()
    update_track_selection(settings.race_ini_path, track_id, layout["config_track"])
    append_command_log(
        "select_layout",
        {
            "track_id": track_id,
            "layout_id": layout_id,
            "layout_config": layout["config_track"],
            "track_name": track["name"],
            "layout_name": layout["name"],
        },
    )

    build_browser_context(message=f"Gespeichert: {track['name']} / {layout['name']}.", message_kind="success")
    return redirect(url_for("cars_placeholder"))


@app.get("/cars")
def cars_placeholder():
    context = build_browser_context(message="Wähle dein Auto.", message_kind="info")
    if not context["selected_track"] or not context["selected_layout"]:
        return redirect(url_for("tracks_overview"))
    return render_template(
        "cars.html",
        **context,
        back_url=url_for("track_detail", track_id=context["selected_track_id"]),
    )


@app.post("/start-game")
def start_game():
    context = build_browser_context()
    if not has_full_selection(context):
        return redirect(url_for("cars_placeholder"))

    settings = load_settings()
    launched, launch_message = launch_game(settings)
    append_command_log(
        "launch",
        {
            "track_id": context["selected_track_id"],
            "layout_id": context["selected_layout_id"] or "default",
            "car_id": context["selected_car"],
            "success": launched,
            "message": launch_message,
        },
    )

    launch_kind = "success" if launched else "warning"
    return redirect(url_for("drive_page", message=launch_message, kind=launch_kind))


@app.post("/stop-game")
def stop_game():
    active_game = read_active_game_process()

    exit_code = os.system("pm2 stop Game")
    clear_active_game_process()
    append_command_log("stop_game", {"mode": "pm2", "result": exit_code})


    return redirect(url_for("tracks_overview"))


@app.get("/drive")
def drive_page():
    message = (request.args.get("message") or "").strip() or "Fahrsteuerung bereit."
    message_kind = (request.args.get("kind") or "info").strip() or "info"
    context = build_browser_context(message=message, message_kind=message_kind)
    if not has_full_selection(context):
        return redirect(url_for("cars_placeholder"))
    return render_template(
        "drive.html",
        **context,
        available_actions=AVAILABLE_ACTIONS,
    )


@app.post("/cars/<car_id>/select")
def select_car(car_id: str):
    context = build_browser_context()
    if not context["selected_track"] or not context["selected_layout"]:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": "Strecke oder Layout fehlt."}), 400
        return redirect(url_for("tracks_overview"))

    car = find_car(context["cars"], car_id)
    if car is None:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": "Auto nicht gefunden."}), 404
        abort(404, description="Auto nicht gefunden.")

    settings = load_settings()
    update_car_selection(settings.race_ini_path, car_id)
    append_command_log(
        "select_car",
        {
            "car_id": car_id,
            "car_name": car["name"],
            "track_id": context["selected_track_id"],
            "layout_id": context["selected_layout_id"] or "default",
        },
    )

    updated_context = build_browser_context(message=f"Auto ausgewählt: {car['name']}", message_kind="success")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "car_id": car_id, "car_name": car["name"]})

    return render_template("cars.html", **updated_context)


@app.get("/assets/tracks/<path:relative_path>")
def track_asset(relative_path: str):
    settings = load_settings()
    if not settings.tracks_root.exists():
        abort(404)
    return send_from_directory(settings.tracks_root, relative_path)


@app.get("/assets/cars/<path:relative_path>")
def car_asset(relative_path: str):
    settings = load_settings()
    if not settings.cars_root.exists():
        abort(404)
    return send_from_directory(settings.cars_root, relative_path)


@app.post("/race")
def race_post():
    settings = load_settings()
    track, car = extract_selection(request.form)

    if not track or not car:
        abort(400, description="Strecke und Auto sind erforderlich.")

    update_race_ini_values(settings.race_ini_path, track, car)
    append_command_log("configure", {"track": track, "car": car, "race_ini_path": str(settings.race_ini_path)})

    message = f"{track} / {car} in {settings.race_ini_path.name} gespeichert."
    context = load_dashboard_context(message=message, message_kind="success")
    return render_template("success.html", **context)


@app.post("/launch")
def launch():
    settings = load_settings()
    track, car = extract_selection(request.form)

    if not track or not car:
        abort(400, description="Strecke und Auto sind erforderlich.")

    update_race_ini_values(settings.race_ini_path, track, car)
    append_command_log("configure", {"track": track, "car": car, "race_ini_path": str(settings.race_ini_path)})

    launched, launch_message = launch_game(settings)
    append_command_log("launch", {"track": track, "car": car, "success": launched, "message": launch_message})

    context = load_dashboard_context(message=launch_message, message_kind="success" if launched else "warning")
    return render_template("success.html", **context)


@app.post("/api/control")
def api_control():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()

    if action not in AVAILABLE_ACTIONS:
        return jsonify({"ok": False, "error": "Unbekannte Aktion."}), 400

    target_key = CONTROL_ACTION_KEYS[action]
    ok, error_message = send_server_keypress(target_key)
    append_command_log(
        "control",
        {"action": action, "key": target_key, "success": ok, "error": error_message if not ok else ""},
    )
    if not ok:
        return jsonify({"ok": False, "error": error_message}), 500

    return jsonify({"ok": True, "action": action, "label": AVAILABLE_ACTIONS[action], "timestamp": utc_now()})


@app.get("/api/state")
def api_state():
    return jsonify(build_browser_context())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)