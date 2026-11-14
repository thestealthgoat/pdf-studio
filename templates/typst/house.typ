// PDF Studio house style.
//
// Grid (US Letter): 1in outer margins; a 1.9in label gutter on the left; running text sits in a
// 4.6in measure (≈ 66 characters at 11pt Source Serif 4). Headings, tables, title blocks and the
// running header/footer span the full 6.5in type area.
//
// Type scale (4 sizes): display 22pt · heading 14pt · body 11pt (serif) / 10pt (sans) · small 8.5pt.

#let gutter = 1.9in
#let measure = 4.6in

#let tokens(brand) = (
  ink: rgb(brand.colors.ink),
  muted: rgb(brand.colors.muted),
  primary: rgb(brand.colors.primary),
  accent: rgb(brand.colors.accent),
  rule: rgb(brand.colors.rule),
  zebra: rgb(brand.colors.zebra),
  paper: rgb(brand.colors.paper),
  sans: brand.fonts.sans,
  serif: brand.fonts.serif,
  body: if brand.fonts.body == "sans" { brand.fonts.sans } else { brand.fonts.serif },
  body-size: if brand.fonts.body == "sans" { 10pt } else { 11pt },
)

// Full-width block: breaks out of the measure into the label gutter.
#let wide(body) = pad(left: -gutter, block(width: 100%, body))

// Small uppercase label (sans 8.5pt, tracked, muted).
#let label(t, brand, color: none) = {
  let k = tokens(brand)
  text(font: k.sans, size: 8.5pt, weight: "semibold", tracking: 0.06em, fill: if color == none { k.muted } else { color }, upper(t))
}

// A row with a hanging label in the gutter and content in the measure.
#let side(lbl, brand, body) = wide(grid(
  columns: (gutter - 0.2in, 0.2in, 1fr),
  align(left, label(lbl, brand)), [], body,
))

#let money(s) = text(number-width: "tabular", s)

// Evaluate a markup string (Typst markup) supplied in the data.
#let md(s) = if s == none or s == "" { [] } else { eval(s, mode: "markup", scope: (wide: wide, measure: measure, gutter: gutter)) }

// Table defaults: no vertical rules, thin horizontal rules, header row repeats, numbers tabular.
#let data-table(brand, columns: (), align: (), header: (), rows: (), footer: none, size: 10pt) = {
  let k = tokens(brand)
  set text(font: k.sans, size: size, number-width: "tabular")
  table(
    columns: columns,
    align: align,
    stroke: (x, y) => if y == 0 { (bottom: 0.75pt + k.ink) } else { (bottom: 0.4pt + k.rule) },
    inset: (x: 5pt, y: 6pt),
    table.header(repeat: true, ..header.map(h => text(size: 8.5pt, weight: "semibold", tracking: 0.04em, fill: k.muted, upper(h)))),
    ..rows,
    ..if footer != none { footer } else { () },
  )
}

// Running header (pages ≥ 2) and footer (all pages).
#let running(brand, title: "", kind: "", rev: none, date: none) = {
  let k = tokens(brand)
  let small(c) = text(font: k.sans, size: 8.5pt, fill: k.muted, c)
  (
    header: context {
      if counter(page).get().first() > 1 {
        wide({
          grid(columns: (1fr, auto), small(title), small(kind + if rev != none [ · Rev #rev] else []))
          v(-4pt)
          line(length: 100%, stroke: 0.4pt + k.rule)
        })
      }
    },
    footer: context wide({
      line(length: 100%, stroke: 0.4pt + k.rule)
      v(-4pt)
      grid(
        columns: (1fr, auto),
        small(brand.name + if date != none [ · #date] else []),
        small[Page #counter(page).display() of #counter(page).final().first()],
      )
    }),
  )
}

// Document shell: metadata, page, type, headings. Every template starts with this.
#let doc(brand: none, title: "", kind: "", author: "", keywords: (), date: none, rev: none, lang: "en", body) = {
  let k = tokens(brand)
  let r = running(brand, title: title, kind: kind, rev: rev, date: date)
  set document(title: title, author: if author == "" { brand.name } else { author }, keywords: keywords)
  set text(font: k.body, size: k.body-size, fill: k.ink, lang: lang, hyphenate: false, number-type: "lining")
  set par(justify: false, leading: 0.72em, spacing: 1.05em)
  set page(
    paper: "us-letter",
    margin: (left: 1in + gutter, right: 1in, top: 1.05in, bottom: 1in),
    header: r.header,
    footer: r.footer,
    header-ascent: 0.35in,
    footer-descent: 0.3in,
    fill: k.paper,
  )
  set strong(delta: 300)
  set list(indent: 0.1in, body-indent: 0.12in, marker: text(fill: k.primary, sym.bullet))
  set enum(indent: 0.1in, body-indent: 0.12in)
  show link: it => text(fill: k.primary, it)
  show heading: set block(sticky: true)
  show heading.where(level: 1): it => wide(block(above: 2.1em, below: 0.9em, {
    set text(font: k.sans, size: 14pt, weight: "semibold", fill: k.primary)
    if it.numbering != none { counter(heading).display(it.numbering); h(0.6em) }
    it.body
  }))
  show heading.where(level: 2): it => block(above: 1.6em, below: 0.7em, {
    set text(font: k.sans, size: 10pt, weight: "semibold", fill: k.ink)
    it.body
  })
  show heading.where(level: 3): it => block(above: 1.3em, below: 0.6em, label(it.body.text, brand))
  show figure.caption: set text(font: k.sans, size: 8.5pt, fill: k.muted)
  show table: set text(font: k.sans, size: 10pt, number-width: "tabular")
  body
}

// First-page title block: logo + brand at left, document facts at right, title below.
#let title-block(brand, kind: "", title: "", subtitle: none, facts: ()) = {
  let k = tokens(brand)
  wide({
    grid(
      columns: (1fr, auto),
      align: (left + horizon, right + top),
      {
        if brand.at("logo", default: "") != "" { box(image("/brands/" + brand.logo, height: 0.42in)); h(0.12in) }
        box(baseline: -0.14in, text(font: k.sans, size: 10pt, weight: "semibold", fill: k.ink, brand.name))
      },
      text(font: k.sans, size: 8.5pt, fill: k.muted, {
        for line in brand.address { line; linebreak() }
        brand.phone; linebreak(); brand.email
      }),
    )
    v(0.38in)
    label(kind, brand, color: k.primary)
    v(-0.05in)
    text(font: k.sans, size: 22pt, weight: "semibold", fill: k.ink, title)
    if subtitle != none and subtitle != "" {
      v(-0.12in)
      text(font: k.sans, size: 11pt, fill: k.muted, subtitle)
    }
    v(0.12in)
    if facts.len() > 0 {
      line(length: 100%, stroke: 0.75pt + k.ink)
      v(0.02in)
      grid(
        columns: facts.len() * (1fr,),
        column-gutter: 0.15in,
        ..facts.map(f => stack(spacing: 4pt, label(f.at(0), brand), text(font: k.sans, size: 10pt, f.at(1)))),
      )
      v(0.04in)
      line(length: 100%, stroke: 0.4pt + k.rule)
    }
    v(0.25in)
  })
}

// Signature / acceptance lines.
#let sign-lines(brand, parties) = {
  let k = tokens(brand)
  let small(c) = text(font: k.sans, size: 8.5pt, fill: k.muted, c)
  wide(grid(
    columns: parties.len() * (1fr,),
    column-gutter: 0.45in,
    ..parties.map(p => grid(
      columns: (1fr, 0.2in, 0.95in),
      row-gutter: 5pt,
      grid.cell(colspan: 3, v(0.32in)),
      line(length: 100%, stroke: 0.6pt + k.ink), [], line(length: 100%, stroke: 0.6pt + k.ink),
      stack(spacing: 4pt, small[Signature], text(font: k.sans, size: 9pt, p)), [], small[Date],
    )),
  ))
}
