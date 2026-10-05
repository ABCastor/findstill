"""Install two user launch agents and a CLI. No Photos configuration is changed."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from .config import runtime


def install(photos_python, port=18473, photos_helper_app=None, photos_library=None):
    if not Path(photos_python).is_file(): raise ValueError("Photos interpreter must exist")
    home = runtime()
    settings_file = home / "settings.json"
    # Keep the virtualenv interpreter path. Resolving its symlink loses installed osxphotos.
    config = json.loads(settings_file.read_text()) if settings_file.is_file() else {}
    if not isinstance(config, dict): raise ValueError("Existing settings must be an object")
    config.update({"photos_python":str(Path(photos_python).absolute()), "port":port})
    if photos_helper_app:
        app = Path(photos_helper_app).absolute()
        if app.suffix != ".app" or not app.is_dir(): raise ValueError("Photos helper must be an existing app bundle")
        config["photos_helper_app"] = str(app)
    if photos_library:
        library = Path(photos_library).absolute()
        if not library.is_dir() or not any((library / "database" / name).is_file() for name in ("Photos.sqlite", "photos.db")):
            raise ValueError("Photos library must contain an existing Photos database")
        config["photos_library"] = str(library)
    cli = Path(sys.executable).parent / "media-search"
    links = Path.home() / ".local/bin"
    link = links / "media-search"
    if link.exists() or link.is_symlink():
        if link.resolve() != cli.resolve(): raise ValueError("An unrelated media-search command already exists")
    agents = Path.home() / "Library/LaunchAgents"
    services = []
    for suffix, args, background in (
        ("server", ["serve", "--port", str(port)], True),
        ("index", ["index"], False),
    ):
        label = "com.media-search."+suffix
        plist = agents / (label+".plist")
        spec = {"Label":label,"ProgramArguments":[str(cli),*args],"RunAtLoad":background,
                "StandardOutPath":str(home/(suffix+".log")),"StandardErrorPath":str(home/(suffix+".log")),
                "EnvironmentVariables":{"TOKENIZERS_PARALLELISM":"false","HF_HUB_OFFLINE":"1","MEDIA_SEARCH_HOME":str(home)},
                # Background I/O throttling can stall the Photos snapshot past its deadline.
                "ProcessType":"Interactive" if suffix == "server" else "Standard"}
        if background: spec["KeepAlive"] = True
        else: spec["StartInterval"] = 21600
        if plist.exists():
            if plistlib.loads(plist.read_bytes()) != spec:
                raise ValueError("Existing service differs; review its configuration before replacing it")
        services.append((plist, spec, label))
    # Check every conflict before touching the settings used by a running service.
    settings_file.write_text(json.dumps(config, indent=2))
    settings_file.chmod(0o600)
    links.mkdir(parents=True, exist_ok=True)
    if not link.exists() and not link.is_symlink(): link.symlink_to(cli)
    agents.mkdir(parents=True, exist_ok=True)
    for plist, spec, label in services:
        if not plist.exists(): plist.write_bytes(plistlib.dumps(spec))
        target = f"gui/{os.getuid()}/{label}"
        existing = subprocess.run(["launchctl","print",target],capture_output=True)
        if existing.returncode:
            subprocess.run(["launchctl","bootstrap",f"gui/{os.getuid()}",str(plist)],check=True)
    return {"dashboard":f"http://127.0.0.1:{port}","cli":str(link),"refresh_hours":6,"runtime":str(home)}
