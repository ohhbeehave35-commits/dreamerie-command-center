"""
Notes and meeting minutes.

Deliberately built on memory.py's "Agent Memory" table instead of a new one: a
note and a remembered fact are the same shape (summary + body + tags + source),
and two near-identical tables drift apart the moment one gains a column. Tags
keep them separable: every row written here carries "note" or "minutes".

Transcription is NOT here. Audio -> text is a paid API and the owner has not
picked a provider, so make_minutes() takes a transcript that already exists
(pasted, uploaded, or from an STT service later). Structuring the transcript is
the part that needed no vendor -- the model does it -- and that is where most of
the value of a meeting-notes product actually lives.
"""

from . import memory


def save_note(text: str, title: str = "", tags: str = "", source: str = "") -> str:
    """Store one note. Returns a confirmation or an honest failure."""
    text = (text or "").strip()
    if not text:
        return "There is nothing in that note to save."
    title = (title or text[:70]).strip()
    tag_str = ("note " + (tags or "")).strip()
    ok, msg = memory.add_memory_checked(
        summary=title, content=text, tags=tag_str, source=source or "note")
    return msg if ok else ("NOT SAVED -- " + msg)


def find_notes(query: str = "", limit: int = 8) -> str:
    """Search saved notes/minutes. Honest about an unreachable store."""
    return memory.recall_memory(query=query, tag="note", limit=limit)


def save_minutes(title: str, minutes: str, attendees: str = "") -> str:
    """Store finished minutes so they survive the conversation."""
    if not (minutes or "").strip():
        return "There are no minutes to save."
    ok, msg = memory.add_memory_checked(
        summary=(title or "Meeting minutes")[:200],
        content=minutes.strip(),
        tags="note minutes",
        source=("attendees: " + attendees) if attendees else "meeting")
    return msg if ok else ("NOT SAVED -- " + msg)
