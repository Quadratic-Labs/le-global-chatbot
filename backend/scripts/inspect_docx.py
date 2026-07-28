import sys
from pathlib import Path

from app.services.docx_parser import parse_docx_sections


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit(
            "Usage: python -m scripts.inspect_docx "
            "<path-to-docx> [output-file]"
        )

    file_path = Path(sys.argv[1])
    output_path = (
        Path(sys.argv[2])
        if len(sys.argv) == 3
        else None
    )

    sections = parse_docx_sections(file_path)

    output_lines: list[str] = [
        f"File: {file_path.name}",
        f"Extracted sections: {len(sections)}",
        "",
    ]

    for index, section in enumerate(
        sections,
        start=1,
    ):
        output_lines.extend(
            [
                "=" * 80,
                f"SECTION {index}",
                f"Main section: {section.section}",
                (
                    "Subsection: "
                    f"{section.subsection or '<none>'}"
                ),
                f"Content length: {len(section.content)}",
                "-" * 80,
                section.content,
                "",
            ]
        )

    output = "\n".join(output_lines)

    if output_path is not None:
        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_text(
            output,
            encoding="utf-8",
        )

        print(
            f"Inspection written to: "
            f"{output_path.resolve()}"
        )
        return

    print(output)


if __name__ == "__main__":
    main()