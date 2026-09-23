"""Resolve the Gmail labels that count as Bluevua KOL Active."""


def cell(row, index):
    return str(row[index]).strip() if len(row) > index else ""


def team_member_names(config_rows):
    expected = ["Member", "Round Robin Order", "Active", "Slack Member ID", "last_round_robin_owner"]
    if len(config_rows) < 3 or config_rows[1][:5] != expected:
        raise RuntimeError("Config team rotation columns changed")
    names = []
    for row in config_rows[2:]:
        name = cell(row, 0)
        if not name:
            break
        names.append(name)
    return names


def resolve_active_labels(gmail_labels, config_rows):
    """Return parent/member Active label IDs, plus the Needs Review ID.

    Only exact member child labels from the Config team table count as Active.
    A member child label also takes assignment priority in the Active sync.
    The Needs Review child label remains separate.
    """
    settings = {cell(row, 0): cell(row, 1) for row in config_rows if len(row) > 1}
    parent_name = settings["gmail_active_label"]
    review_name = parent_name + "/" + settings["gmail_needs_review_label"]
    by_name = {item["name"].casefold(): item["id"] for item in gmail_labels}

    wanted = [("", parent_name)] + [(member, parent_name + "/" + member)
                                     for member in team_member_names(config_rows)]
    missing = [name for _, name in wanted if name.casefold() not in by_name]
    if review_name.casefold() not in by_name:
        missing.append(review_name)
    if missing:
        raise RuntimeError("Missing Gmail labels: " + ", ".join(missing))

    owner_by_label_id = {by_name[name.casefold()]: owner for owner, name in wanted}
    active_ids = set(owner_by_label_id)
    return {
        "parent_name": parent_name,
        "active_ids": active_ids,
        "owner_by_label_id": owner_by_label_id,
        "review_id": by_name[review_name.casefold()],
    }
