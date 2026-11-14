// Internal memo.
#import "house.typ": *

#let memo(data: none, brand: none) = {
  let k = tokens(brand)
  show: doc.with(brand: brand, title: data.subject, kind: "Memo", author: data.from, date: data.at("date", default: none))
  title-block(brand, kind: "Memo", title: data.subject, facts: ())
  for (l, key) in (("To", "to"), ("From", "from"), ("cc", "cc"), ("Date", "date")) {
    let val = data.at(key, default: none)
    if val != none and val != "" and val != () {
      side(l, brand, if type(val) == array { val.join(", ") } else { val })
      v(-0.02in)
    }
  }
  wide(line(length: 100%, stroke: 0.4pt + k.rule))
  v(0.1in)
  md(data.body)
}
