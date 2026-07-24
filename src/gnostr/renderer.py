class ContentRenderer:
    # Fixed regex: matches URLs even when preceded by whitespace/newlines
    LINK_REGEX = re.compile(r"(?:^|\s)((?:https?://|nostr:)[^\s]+)")
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    VIDEO_EXTS = {".mp4", ".mov", ".webm", ".gif"}