"""Executed by the separately installed osxphotos interpreter. Never exports originals."""
import json
import os
from pathlib import Path
import sys

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".avif", ".bmp"}


def local_previews(paths):
    images = [p for p in paths if p and Path(p).suffix.lower() in IMAGE_SUFFIXES and Path(p).is_file()]
    return sorted(images, key=lambda p: os.stat(p).st_size, reverse=True)


def snapshot(library_path=None):
    import osxphotos
    # Avoid app-container preference discovery when the launch agent lacks access.
    db = osxphotos.PhotosDB(dbfile=library_path)
    rows = []
    excluded = 0
    for photo in db.photos():
        if photo.hidden or photo.intrash:
            excluded += 1
            continue
        # Prefer the largest available local derivative, without requesting iCloud data.
        paths = local_previews(photo.path_derivatives)
        path = paths[0] if paths else None
        place = photo.place
        rows.append({
            "uuid": photo.uuid, "filename": photo.original_filename or photo.filename,
            "date": photo.date.isoformat(), "persons": list(photo.persons),
            "place": str(place.name or "") if place else "",
            "media_type": "video" if photo.ismovie else "image", "path": path, "paths": paths,
            "modified": photo.date_modified.isoformat() if photo.date_modified else None,
        })
    return {"assets": rows, "excluded": excluded, "provider": "osxphotos", "version": osxphotos.__version__}


if __name__ == "__main__":
    destination = Path(sys.argv[1])
    result = snapshot(sys.argv[2] if len(sys.argv) > 2 else None)
    temp = destination.with_suffix(".tmp")
    temp.write_text(json.dumps(result))
    os.chmod(temp, 0o600)
    temp.replace(destination)
