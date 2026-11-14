// Business letter on letterhead: labels (To, Re) hang in the gutter, body in the measure.
#import "house.typ": *

#let letter(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(brand: brand, title: data.at("subject", default: "Letter"), kind: "Letter",
    author: data.at("signer", default: (name: "")).name, date: data.at("date", default: none))

  // Letterhead (no document-kind title: letters open with the address block)
  wide({
    grid(columns: (1fr, auto), align: (left + horizon, right + top),
      lockup(brand),
      text(font: k.sans, size: 8.5pt, fill: k.muted, contact(brand)))
    v(0.6in)
  })
  side("Date", brand, data.date)
  v(0.05in)
  side("To", brand, {
    let r = data.recipient
    r.name
    if r.at("title", default: "") != "" { linebreak(); r.title }
    if r.at("org", default: "") != "" { linebreak(); r.org }
    for l in r.at("address", default: ()) { linebreak(); l }
  })
  if data.at("subject", default: "") != "" { v(0.05in); side("Re", brand, strong(data.subject)) }
  v(0.25in)
  [#data.at("salutation", default: "Dear " + data.recipient.name + ",")]
  parbreak()
  md(data.body)
  v(0.2in)
  block(breakable: false, {
    [#data.at("closing", default: "Sincerely,")]
    v(0.55in)
    let sg = data.at("signer", default: (name: ""))
    text(weight: "semibold", sg.name)
    if sg.at("title", default: "") != "" { linebreak(); text(fill: k.muted, sg.title) }
  })
  if data.at("enclosures", default: ()).len() > 0 { v(0.2in); side("Enclosures", brand, data.enclosures.join(linebreak())) }
  if data.at("cc", default: ()).len() > 0 { v(0.05in); side("cc", brand, data.cc.join(linebreak())) }
}
