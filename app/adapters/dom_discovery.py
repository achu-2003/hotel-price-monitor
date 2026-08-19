"""Finding the room list in a rendered page, when there is no JSON to read.

WHY THIS IS NEEDED
==================
JSON discovery covers modern booking engines, and misses an entire class of
site: the ones that render prices straight into HTML. Those are common among
exactly the small independent hotels this system exists to watch, and eZee's
own booking pages do it too — a real hotel failed with "DOM extraction needs
room_card" because the config assumed an API that page never calls.

THE IDEA
========
A price on a booking page is never alone. It sits inside a card that also holds
the room's name, and that card is repeated once per room. So rather than
guessing at class names, this looks for the repetition:

1. find every element whose own text is a price
2. walk up its ancestors, recording each one's "signature" (tag + classes)
3. the signature that appears several times, each containing exactly one price,
   is the room card
4. inside one card, the price element and the most name-like text give the two
   selectors that matter

Repetition is what makes it reliable. A phone number, a pincode or a review
count can look like a price once; they do not appear four times in four
identically-structured cards each next to a room name.

The work happens in the browser via ``page.evaluate`` because walking a DOM is
what JavaScript is for. Python scores the result and — as with JSON discovery —
refuses anything whose prices are not visible on the page.
"""
from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

log = get_logger("discovery.dom")

#: Executed inside the page. Returns candidate room cards with the selectors
#: needed to read them again, ranked by how convincingly they repeat.
_FIND_CARDS_JS = r"""
() => {
  // The unmarked branch MUST be able to span a thousands separator. Written as
  // \d{3,6} it could not, so "3,200.00" -- with its "Rs" in a sibling element,
  // as eZee renders it -- matched only the "200" after the comma and reported
  // a 3,200 rupee room as costing 200. Wrong by a factor of sixteen, in range,
  // and verifiable against the page, because 200 really is printed there.
  // Grouped forms are therefore matched whole and preferred over bare runs.
  const PRICE = /(?:₹|Rs\.?|INR)\s*([\d,]{3,}(?:\.\d{1,2})?)|\b(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{3,6}(?:\.\d{1,2})?)\b/g;
  const ROOMY = /\b(room|suite|deluxe|standard|superior|premium|executive|cottage|villa|studio|tent|dorm|family|double|twin|triple|single|apartment|bed|queen|king|balcony|view|ac)\b/i;

  const ownText = (el) => {
    let out = "";
    for (const node of el.childNodes) {
      if (node.nodeType === 3) out += node.nodeValue;
    }
    return out.trim();
  };

  // A bare four-digit number in the year range is a year far more often than
  // it is a room rate. The first real site this ran against returned 2026 for
  // every room -- the year out of a date cell, sitting in the same table as
  // the prices. A genuine rate of that size is written with a currency marker,
  // so a marked number is trusted and a bare one in that window is not.
  const looksLikeYear = (n, marked) => !marked && n >= 1900 && n <= 2100;

  // EVERY number in the text is considered, not just the first one.
  //
  // Reading only the first match meant a single unrelated number at the top of
  // a card discarded the card entirely. On a real page the room rows begin
  // with the stay date -- "19/08/2026" -- so the first match was 2026, which
  // is correctly refused as a year, and the container holding "Rs 3,200.00"
  // three lines below was then recorded as holding no price at all. Three
  // room rows on a working page, invisible, because of a date.
  //
  // A marked number wins outright wherever it appears: "Rs 3,200" is a price
  // in a way that a bare 3200 next to it can never be proven to be.
  const parsePrice = (text) => {
    PRICE.lastIndex = 0;
    let match, unmarked = null;
    while ((match = PRICE.exec(text)) !== null) {
      const marked = match[1] !== undefined;   // the ₹ / Rs / INR branch
      const n = parseFloat((match[1] || match[2] || "").replace(/,/g, ""));
      if (!(n >= 100 && n <= 500000)) continue;      // phone, pincode, review count
      if (looksLikeYear(n, marked)) continue;
      if (marked) { PRICE.lastIndex = 0; return n; }
      if (unmarked === null) unmarked = n;
    }
    PRICE.lastIndex = 0;
    return unmarked;
  };

  // Does this page write its prices with a currency marker at all? If so, only
  // marked numbers are considered: on such a page an unmarked number is
  // something else, and there is no reason to guess.
  const pageHasMarkedPrices = /(?:₹|Rs\.?|INR)\s*[\d,]{3,}/.test(document.body.innerText || "");

  const MARKER = /(?:₹|Rs\.?|INR)/;

  // The marker frequently is not in the same element as the number. eZee
  // renders "<p class=pre_currency>Rs</p><span class=rate2>3,200.00</span>",
  // and requiring both in one leaf rejected every price on that page -- 43 of
  // them -- reporting "no prices found" for a page covered in prices. So the
  // number is trusted when the marker sits anywhere in its immediate
  // neighbourhood: its own text, its parent's, or the text just before it.
  const nearbyMarked = (el) => {
    if (MARKER.test(el.textContent || "")) return true;
    const parent = el.parentElement;
    if (parent && MARKER.test(parent.textContent || "")) return true;
    const grandparent = parent && parent.parentElement;
    if (grandparent && (grandparent.textContent || "").length < 200
        && MARKER.test(grandparent.textContent || "")) return true;
    return false;
  };

  const parsePriceStrict = (text, el) => {
    if (pageHasMarkedPrices && !MARKER.test(text) && !(el && nearbyMarked(el))) return null;
    return parsePrice(text);
  };

  // Classes that a build tool generated. styled-components ("sc-c8jr3n-0"),
  // emotion ("css-1x2y3z"), JSS -- all of them rotate on the next deploy. A
  // selector built from one works perfectly today and silently matches
  // nothing next week, which is the worst failure this system can have: a
  // stale price that still looks live. They are refused, and the scan falls
  // back to something durable.
  const isGeneratedClass = (c) =>
    /^(sc|css|jss|makeStyles|emotion)[-_]/i.test(c) ||
    (/\d/.test(c) && /[a-z]/i.test(c) && c.length >= 5 && !/[-_]/.test(c));

  // An id ending in an index -- "rt-info_0", "row_3" -- identifies ONE card,
  // not the shape of a card. Useless for matching all of them, and misleading
  // if stored.
  const isPerInstanceId = (v) => /[-_]?\d+$/.test(v);

  const signature = (el) => {
    const cls = (el.className && typeof el.className === "string")
      ? el.className.trim().split(/\s+/).filter(c =>
          c && !/^(ng|is|has)-/.test(c) && !/\d{3,}/.test(c)
          && !isGeneratedClass(c)).slice(0, 3)
      : [];
    return el.tagName.toLowerCase() + (cls.length ? "." + cls.join(".") : "");
  };

  // Playwright's text engine matches the SMALLEST element containing the
  // match, which is what makes it usable where every class is generated: it
  // lands on the leaf holding the price rather than on the card that contains
  // it. Built from what this element actually says, so it describes a shape
  // rather than one night's number.
  const textSelectorFor = (el) => {
    const t = ownText(el);
    if (!t || t.length > 60) return null;
    // Written WITHOUT backslashes on purpose. These are strings that have to
    // survive as regular expressions through a JS string literal, a JSON
    // round-trip and a database column, and a lone backslash is eaten at the
    // first of those -- shipping "text=/^s*.../", which matches nothing and
    // says nothing about why. "[ ]" for whitespace, "[0-9]" for digits and
    // "[.]" for a literal dot need no escaping anywhere, and no escape can be
    // lost that was never written.
    if (/(?:₹|Rs\.?|INR)/.test(t)) return "text=/^[ ]*(?:₹|Rs[.]?|INR)[ ]?[0-9.,]+[ ]*$/";
    if (/^[\d.,]+$/.test(t)) return "text=/^[ ]*[0-9][0-9.,]*[ ]*$/";
    const word = (t.match(ROOMY) || [])[0];
    if (word) return "text=/" + word + "/i";
    return null;
  };

  const selectorFor = (el) => {
    // A hand-written test hook beats anything inferred: it exists precisely to
    // be selected on, and it is the one thing on a hashed-class page that the
    // site intends to keep stable. Checked BEFORE classes for that reason.
    for (const attr of ["data-testid", "data-test", "data-qa", "data-id", "itemprop"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v && !/\d{4,}/.test(v)) return `${el.tagName.toLowerCase()}[${attr}="${v}"]`;
    }
    const id = el.getAttribute && el.getAttribute("id");
    // "rt-info_0" and "row_3" name ONE card rather than the shape of a card:
    // stored as a selector they would monitor the first room forever and
    // silently ignore the rest.
    if (id && !/\d{4,}/.test(id) && !isPerInstanceId(id)) {
      return `${el.tagName.toLowerCase()}#${id}`;
    }
    return signature(el);
  };


  // One room card in, its name and its rate out.
  //
  // Taking the FIRST match of each on a real page gave the room a name of
  // "Room Rates Exclusive of Tax" and a rate of 200 -- a column heading and an
  // extra-person charge. A booking card is full of text that resembles what we
  // want, and exactly one of each is the thing itself, so both are scored.
  const context = (el) => {
    const own = ((el.className || "") + " " + (el.id || "")).toString();
    const parent = el.parentElement
      ? ((el.parentElement.className || "") + " " + (el.parentElement.id || "")).toString()
      : "";
    return (own + " " + parent).toLowerCase();
  };

  const pickFrom = (card) => {
    let priceEl = null, nameEl = null, nameText = null, priceValue = null;
    let bestNameScore = -1, bestPriceScore = -1, bestPriceValue = -1;

    for (const el of card.querySelectorAll("*")) {
      if (el.children.length > 2) continue;
      const t = ownText(el);
      if (!t) continue;
      const asPrice = parsePriceStrict(t, el);

      if (asPrice !== null && t.length <= 40) {
        const where = context(el);
        let score = 0;
        if (MARKER.test(t)) score += 3;                       // says so itself
        if (/rate|price|amt|amount|tariff|cost/.test(where)) score += 3;
        // A struck-through "was" price is visible on the page and so passes
        // verification just as well as the real one; it has to be excluded by
        // what it IS, not by whether it can be found.
        if (/strike|struck|old|was|mrp|orig|rack|discount|save|off/.test(where)) score -= 6;
        if (/extra|addon|add-on|child|person|tax|fee/.test(where)) score -= 4;
        // Between two equally plausible numbers the larger is the room and the
        // smaller is a supplement -- an extra bed is never dearer than the bed.
        if (score > bestPriceScore || (score === bestPriceScore && asPrice > bestPriceValue)) {
          bestPriceScore = score;
          bestPriceValue = asPrice;
          priceEl = el;
          priceValue = asPrice;
        }
        continue;
      }

      if (t.length < 3 || t.length > 80) continue;
      if (asPrice !== null || !ROOMY.test(t)) continue;
      if (!/[A-Za-z]{3}/.test(t)) continue;
      if (/^\d/.test(t)) continue;                            // "1 Room"
      // Headings and small print that happen to contain a room word.
      if (/\b(per|left|available|select|choose|remaining|total|from|starting|guests?|adults?|nights?)\b/i.test(t)) continue;
      if (/\b(rates?|tax|taxes|inclusive|exclusive|prices?|policy|policies|cancellation|refundable|check-?in|check-?out|capacity)\b/i.test(t)) continue;
      const where = context(el);
      const words = t.split(/\s+/).length;
      let score = (words >= 2 ? 10 : 0) + Math.min(t.length, 40);
      if (/name|title|heading|roomtype|room-type|rmname/.test(where)) score += 20;
      if (/^h[1-6]$/i.test(el.tagName)) score += 10;
      if (score > bestNameScore) {
        bestNameScore = score;
        nameEl = el;
        nameText = t;
      }
    }
    return { priceEl, nameEl, nameText, priceValue };
  };

  // 1. every element whose OWN text is a price
  const priceEls = [];
  for (const el of document.querySelectorAll("body *")) {
    if (el.children.length > 2) continue;
    const t = ownText(el);
    if (!t || t.length > 40) continue;
    const value = parsePriceStrict(t, el);
    if (value !== null) priceEls.push({ el, value, text: t });
  }
  if (!priceEls.length) return { cards: [], priceCount: 0 };

  // 2/3. walk up, and count how often each ancestor signature repeats
  const groups = new Map();
  for (const { el, value } of priceEls) {
    let node = el.parentElement, depth = 0;
    while (node && depth < 6 && node.tagName !== "BODY") {
      const sig = selectorFor(node);
      if (sig.includes(".") || sig.includes("[") || sig.includes("#")) {
        if (!groups.has(sig)) groups.set(sig, { nodes: new Set(), prices: [] });
        const g = groups.get(sig);
        if (!g.nodes.has(node)) { g.nodes.add(node); g.prices.push(value); }
      }
      node = node.parentElement;
      depth++;
    }
  }

  // 4. for each candidate signature, find the name and price inside one card
  //
  // Repetition used to be required here -- "a card repeats; a banner does
  // not". It is good evidence and it is not available everywhere: a hotel
  // detail page that shows its default room and hides the rest behind "View
  // All Rooms" has exactly ONE card, and so does a small property with one
  // room type. Demanding two found nothing on those pages and reported it as
  // "no room list", which reads as "this site is unreadable" rather than
  // "this site has one room".
  //
  // So a single card is now allowed through, and the burden moves to the
  // check that was always the real one: Python refuses any candidate whose
  // prices are not also visible on the page. Repetition still WINS -- it
  // sorts first -- it just no longer excludes.
  const cards = [];
  for (const [sig, g] of groups) {
    const nodes = [...g.nodes];
    const withPrice = nodes.filter(n => parsePriceStrict(n.textContent || "", n) !== null);
    if (!withPrice.length) continue;

    const sample = withPrice[0];
    const picked = pickFrom(sample);
    let priceEl = picked.priceEl, nameEl = picked.nameEl, nameText = picked.nameText;
    if (!priceEl || !nameEl) continue;

    // A class-based selector when the classes are real; a text-based one when
    // they are generated hashes. The second is not a lesser option here --
    // on a styled-components site it is the only one that will still work
    // after the next deploy.
    const usable = (s) => s && (s.includes(".") || s.includes("[") || s.startsWith("text="));
    let priceSel = selectorFor(priceEl);
    if (!usable(priceSel)) priceSel = textSelectorFor(priceEl);
    let nameSel = selectorFor(nameEl);
    if (!usable(nameSel)) nameSel = textSelectorFor(nameEl);
    if (!usable(priceSel) || !usable(nameSel)) continue;

    // Each card is read on its own terms. Resolving the SAMPLE's elements for
    // every card reported one room's price three times -- three rows, three
    // identical numbers, and a verification step with nothing to catch.
    const names = [], prices = [];
    for (const card of withPrice.slice(0, 10)) {
      const found = pickFrom(card);
      const n = found.nameText;
      const p = found.priceValue;
      if (n && p !== null) { names.push(n); prices.push(p); }
    }
    if (!names.length) continue;

    cards.push({
      card: sig,
      name_selector: nameSel,
      price_selector: priceSel,
      count: withPrice.length,
      matched: names.length,
      names: names.slice(0, 8),
      prices: prices.slice(0, 8),
      sample_name: nameText,
      // Sorting hints, not findings.
      anchored: sig.includes("[") || sig.includes("#") ? 1 : 0,
      cardLen: (sample.textContent || "").trim().length,
    });
  }

  // Most cards matched first, then a stable anchor over a class list, then the
  // tightest card -- a container holding one room beats one holding the page.
  cards.sort((a, b) =>
    (b.matched - a.matched) || (b.anchored - a.anchored) ||
    (a.cardLen - b.cardLen) || (b.count - a.count));
  return { cards: cards.slice(0, 5), priceCount: priceEls.length };
}
"""


def find_room_cards(page) -> list[dict[str, Any]]:
    """Candidate room cards in the rendered page, best first.

    Never raises: a page that defeats this should fall back to "nothing found",
    not take the whole inspection down.
    """
    try:
        result = page.evaluate(_FIND_CARDS_JS)
    except Exception as exc:  # noqa: BLE001
        log.warning("dom_scan_failed", error=str(exc)[:200])
        return []

    cards = (result or {}).get("cards") or []
    log.info(
        "dom_scan",
        prices_on_page=(result or {}).get("priceCount", 0),
        candidates=len(cards),
        best=cards[0]["card"] if cards else None,
    )
    return cards
