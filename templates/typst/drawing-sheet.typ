// Drawing sheet with title block (ANSI A–E, ARCH A–E). Drawing content: an image/SVG (vector) or notes.
#import "house.typ": *

#let sizes = (
  "ansi-a": (11in, 8.5in), "ansi-b": (17in, 11in), "ansi-c": (22in, 17in), "ansi-d": (34in, 22in), "ansi-e": (44in, 34in),
  "arch-a": (12in, 9in), "arch-b": (18in, 12in), "arch-c": (24in, 18in), "arch-d": (36in, 24in), "arch-e": (48in, 36in),
)

#let drawing_sheet(data: none, brand: none) = {
  let k = tokens(brand)
  let (W, H) = sizes.at(lower(data.at("size", default: "arch-d")))
  let s = calc.max(0.55, W / 36in)  // scale text with sheet size; 1.0 on ARCH D
  set document(title: data.sheet + " " + data.title, author: brand.name, keywords: ("drawing", data.sheet))
  set page(width: W, height: H, margin: 0.5in, fill: k.paper)
  set text(font: k.sans, size: 9pt * s, fill: k.ink, lang: "en")
  let tbw = 4in * s
  let small(t) = text(size: 7pt * s, fill: k.muted, upper(t))
  rect(width: 100%, height: 100%, stroke: 1.6pt + k.ink, inset: 0pt, grid(
    columns: (1fr, tbw),
    // drawing area
    box(width: 100%, height: 100%, inset: 0.35in * s, {
      if data.at("image", default: "") != "" { align(center + horizon, image("/assets/" + data.image, width: 100%, height: 100%, fit: "contain")) }
      if data.at("notes", default: ()).len() > 0 {
        text(size: 14pt * s, weight: "semibold", data.at("notes_title", default: "GENERAL NOTES"))
        v(0.12in * s)
        set text(size: 12pt * s)  // 1/8 in at print size
        set enum(spacing: 0.9em)
        enum(..data.notes.map(n => upper(n)))
      }
    }),
    // title block
    grid.cell(stroke: (left: 1.2pt + k.ink), box(width: 100%, height: 100%, {
      let cell(lbl, val, size: 10pt) = block(width: 100%, inset: 0.08in * s, stroke: (bottom: 0.5pt + k.rule),
        stack(spacing: 3pt * s, small(lbl), text(size: size * s, weight: "semibold", val)))
      block(width: 100%, inset: 0.12in * s, stroke: (bottom: 0.8pt + k.ink), {
        if brand.at("logo", default: "") != "" { image("/brands/" + brand.logo, height: 0.45in * s); v(0.06in * s) }
        text(size: 11pt * s, weight: "semibold", brand.name)
        linebreak()
        text(size: 7.5pt * s, fill: k.muted, brand.address.join(" · "))
      })
      cell("Project", data.project, size: 11pt)
      if data.at("client", default: "") != "" { cell("Client", data.client) }
      // revision table
      block(width: 100%, inset: 0.08in * s, stroke: (bottom: 0.5pt + k.rule), {
        small("Revisions")
        v(2pt)
        set text(size: 7.5pt * s, number-width: "tabular")
        table(columns: (0.35in * s, 0.8in * s, 1fr), stroke: none, inset: (x: 2pt, y: 2.5pt),
          table.header(text(weight: "semibold", "REV"), text(weight: "semibold", "DATE"), text(weight: "semibold", "DESCRIPTION")),
          ..data.at("revisions", default: ()).map(r => (str(r.rev), r.date, upper(r.description))).flatten())
      })
      v(1fr)
      grid(columns: (1fr, 1fr),
        cell("Drawn", data.at("drawn", default: "")), cell("Checked", data.at("checked", default: "")),
        cell("Date", data.at("date", default: "")), cell("Scale", data.at("scale", default: "AS NOTED")))
      cell("Sheet title", upper(data.title), size: 11pt)
      block(width: 100%, inset: 0.1in * s, grid(columns: (1fr, auto), align: (left + bottom, right + bottom),
        stack(spacing: 3pt * s, small("Sheet"), text(size: 30pt * s, weight: "bold", data.sheet)),
        stack(spacing: 3pt * s, small("Rev"), text(size: 16pt * s, weight: "semibold", str(data.at("rev", default: "0"))))))
    })),
  ))
}
