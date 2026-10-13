"""Contextual OpenAI document generation when no user template fits."""

from app.services.extractor import ExtractionError, GeneratedNote, generate_note


class GeneralNoteError(ExtractionError):
    """A contextual document was not generated; never substitute raw text."""


async def generate_general_note(transcription: str) -> GeneratedNote:
    try:
        return await generate_note(transcription)
    except ExtractionError as exc:
        raise GeneralNoteError(str(exc)) from exc
