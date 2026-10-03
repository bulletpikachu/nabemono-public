from pathlib import Path

from src.config import AFFECTION_PATH


AFFECTION_FILE = Path(AFFECTION_PATH)


def ensure_affection_file():
    AFFECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    AFFECTION_FILE.touch(exist_ok=True)


def affection_formula(delta, affection):
    print(f"Calculating affection change: delta={delta}, current_affection={affection}")
    adjusted_delta = float(delta) / 2
    print(f"Adjusted affection change: {adjusted_delta}")
    return adjusted_delta


def update_affection(user, delta):
    ensure_affection_file()

    lines = AFFECTION_FILE.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    found = False

    for line in lines:
        if not line.strip():
            continue

        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) < 2:
            updated_lines.append(line)
            continue

        name, value = parts
        if name == user:
            found = True
            try:
                value_num = float(value)
            except ValueError:
                value_num = 50

            value_num += affection_formula(delta, value_num)
            updated_lines.append(f"{name},{value_num}")
        else:
            updated_lines.append(line)

    if not found:
        updated_lines.append(f"{user},50,0")

    AFFECTION_FILE.write_text("\n".join(updated_lines) + ("\n" if updated_lines else ""), encoding="utf-8")

def set_affection(user, new_value):
    ensure_affection_file()

    lines = AFFECTION_FILE.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    found = False

    for line in lines:
        if not line.strip():
            continue

        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) < 2:
            updated_lines.append(line)
            continue

        name, value = parts
        if name == user:
            found = True
            try:
                value_num = float(value)
            except ValueError:
                value_num = 50

            value_num = new_value
            updated_lines.append(f"{name},{value_num}")
        else:
            updated_lines.append(line)

    if not found:
        updated_lines.append(f"{user},50,0")

    AFFECTION_FILE.write_text("\n".join(updated_lines) + ("\n" if updated_lines else ""), encoding="utf-8")

def get_affection(user):
    ensure_affection_file()

    lines = AFFECTION_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue

        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) < 2:
            continue

        name, value = parts
        if name == user:
            try:
                return float(value)
            except ValueError:
                return 50.0

    return 50.0


def affection_ranking():
    ensure_affection_file()

    rankings = []
    lines = AFFECTION_FILE.read_text(encoding="utf-8").splitlines()

    for line in lines:
        if not line.strip():
            continue

        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) < 2:
            continue

        name, value = parts
        try:
            value_num = float(value)
        except ValueError:
            value_num = 50.0

        rankings.append((name, value_num))

    return sorted(rankings, key=lambda entry: entry[1], reverse=True)


def affection_check(affection):
    affection_state = "hate"

    if affection > 10.0:
        affection_state = "slight_hate"
    if affection > 20.0:
        affection_state = "dislike"
    if affection > 30.0:
        affection_state = "slight_dislike"
    if affection > 40.0:
        affection_state = "neutral"
    if affection > 60.0:
        affection_state = "slight_like"
    if affection > 70.0:
        affection_state = "like"
    if affection > 80.0:
        affection_state = "slight_love"
    if affection > 90.0:
        affection_state = "love"

    return affection_state

def get_users():
    ensure_affection_file()

    users = []
    lines = AFFECTION_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue

        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) < 2:
            continue

        name, value = parts
        users.append(name)

    return users