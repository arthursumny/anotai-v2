"""AnotAI — seed do marketplace: importa templates/*.json como públicos (user_id=None)."""

import asyncio
import json
import sys
from pathlib import Path

from sqlmodel import select

from app.core.database import async_session_factory
from app.models.template import Template

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


async def main() -> None:
    files = sorted(TEMPLATES_DIR.glob("*.json"))
    if not files:
        print("Nenhum template em templates/")
        return
    async with async_session_factory() as session:
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            title = data.get("title", path.stem)
            existing = await session.exec(select(Template).where(Template.title == title))
            if existing.first() is not None:
                print(f"SKIP {title} (já existe)")
                continue
            tpl = Template(
                user_id=None,
                title=title,
                purpose=data.get("purpose", ""),
                semantic_trigger=data.get("semantic_trigger", ""),
                example_output=data.get("example_output", ""),
                output_schema=data.get("output_schema", {"type": "object"}),
                actions=data.get("actions", []),
                is_public=bool(data.get("is_public", True)),
            )
            session.add(tpl)
            print(f"OK {title}")
        await session.commit()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
