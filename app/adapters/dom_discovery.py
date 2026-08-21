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

  // Invisible formatting characters are stripped before ANY matching.
  //
  // One site wraps its currency symbol in Unicode directional isolates, so
  // "₹2,017" arrives as "<isolate>Rs<isolate> 2,017". The symbol is then no longer
  // adjacent to the digits, the marked-price branch of PRICE cannot match, and
  // 2,017 is left looking like a bare number -- inside the year range, where
  // bare numbers are deliberately refused. A real ₹2,017 room silently became
  // "the year 2017" and vanished from the room list, while its ₹4,381
  // neighbour survived purely because that is not a plausible year.
  //
  // Zero-width spaces, bidi marks and BOMs do the same damage anywhere they
  // land, so all of them go before the text is read, not after.
  const INVISIBLE = new RegExp("[\u00ad\u200b-\u200f\u2060-\u206f\ufeff]", "g");
  const NBSP = new RegExp("\u00a0", "g");
  const clean = (t) => (t || "").replace(INVISIBLE, "").replace(NBSP, " ");

  const ownText = (el) => {
    let out = "";
    for (const node of el.childNodes) {
      if (node.nodeType === 3) out += node.nodeValue;
    }
    return clean(out).trim();
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
  const pageHasMarkedPrices = /(?:₹|Rs\.?|INR)\s*[\d,]{3,}/.test(clean(document.body.innerText));

  const MARKER = /(?:₹|Rs\.?|INR)/;

  // The marker frequently is not in the same element as the number. eZee
  // renders "<p class=pre_currency>Rs</p><span class=rate2>3,200.00</span>",
  // and requiring both in one leaf rejected every price on that page -- 43 of
  // them -- reporting "no prices found" for a page covered in prices. So the
  // number is trusted when the marker sits anywhere in its immediate
  // neighbourhood: its own text, its parent's, or the text just before it.
  const nearbyMarked = (el) => {
    if (MARKER.test(clean(el.textContent))) return true;
    const parent = el.parentElement;
    if (parent && MARKER.test(clean(parent.textContent))) return true;
    const grandparent = parent && parent.parentElement;
    if (grandparent && (grandparent.textContent || "").length < 200
        && MARKER.test(clean(grandparent.textContent))) return true;
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
    // NOT anchored, deliberately. The scan sees text with its invisible
    // characters removed; Playwright, later, does not. A site that renders
    // "<isolate>Rs<isolate> 2,017" starts its price element with a character
    // that ^[ ]* cannot cross, so an anchored pattern found every room card
    // and then no price inside any of them. The currency marker already makes
    // this specific, and Playwright's text engine returns the SMALLEST
    // matching element, so the anchors were never what made it precise.
    //
    // "[^0-9]{0,3}" rather than "[ ]?" for the gap after the symbol: the
    // isolate character sits BETWEEN the symbol and the space, so a pattern
    // expecting only an optional space still could not cross it. Anything
    // short and non-numeric is allowed through instead, which covers a space,
    // a non-breaking space, an isolate, or any two of them together.
    if (/(?:₹|Rs\.?|INR)/.test(t)) return "text=/(?:₹|Rs[.]?|INR)[^0-9]{0,3}[0-9][0-9.,]*/";
    // The bare-number form keeps its anchors: with no currency marker to
    // narrow it, an unanchored digit pattern would match "9 Rooms Left".
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
    const own = signature(el);
    if (own.includes(".")) return own;

    // The element itself is bare, but its wrapper is not: room names are
    // frequently an unclassed <h2> inside <div class="roomName">. Falling
    // straight through to a text selector here produced "text=/Standard/i" --
    // built from the FIRST card's room name, and therefore matching nothing in
    // the second. Every room after the first vanished from a page that listed
    // them all. The wrapper's class describes the shape instead of one room.
    const parent = el.parentElement;
    if (parent) {
      const parentSig = signature(parent);
      if (parentSig.includes(".")) {
        return `${parentSig} > ${el.tagName.toLowerCase()}`;
      }
    }
    return own;
  };


  // One room card in, its name and its rate out.
  //
  // Taking the FIRST match of each on a real page gave the room a name of
  // "Room Rates Exclusive of Tax" and a rate of 200 -- a column heading and an
  // extra-person charge. A booking card is full of text that resembles what we
  // want, and exactly one of each is the thing itself, so both are scored.
  // Class names are matched as WHOLE WORDS. Substring matching is what broke
  // this on the first Tailwind site it met.
  //
  // "font-bold" contains "old", so the struck-through-price test fired on
  // every bold element on the page -- which, on a booking card, is the price
  // itself. The genuine rate was scored as a stale "old price" and lost to the
  // tax line printed beneath it, and a six-room hotel was recorded as having
  // one room costing its own tax. "off" inside "offer", "was" inside
  // "washroom" and "save" inside "saver" all fail the same way.
  //
  // Splitting on every non-alphanumeric boundary turns "font-bold
  // roomtype-price" into " font bold roomtype price ", where /\bold\b/ can no
  // longer match and /\bprice\b/ still does. Every pattern tested against this
  // string must therefore be \b-anchored, and hyphenated names must be written
  // with a space ("room type"), because that is what they become here.
  const tokens = (value) =>
    (value || "").toString().toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  const context = (el) => {
    const own = tokens(el.className).concat(tokens(el.id));
    const parent = el.parentElement
      ? tokens(el.parentElement.className).concat(tokens(el.parentElement.id))
      : [];
    // Leading and trailing spaces so \b behaves at both ends.
    return " " + own.concat(parent).join(" ") + " ";
  };

  // Text that says the number beside it is an ADD-ON rather than the rate.
  // Read from the element's own text, which is the one thing a site cannot
  // rename: "+ ₹169.5 in taxes and charges" is a tax line whatever the div
  // holding it is called.
  const SUPPLEMENT =
    /\b(tax|taxes|gst|vat|fee|fees|charge|charges|extra|surcharge|deposit|additional|per person|per head|per adult|per child)\b/i;
  // ...unless the number is the all-in figure, which mentions tax for the
  // opposite reason. "₹3,559 incl. taxes" is the price, not a supplement.
  const INCLUSIVE_TEXT = /\b(incl|inclusive|including|included|all in|all inclusive)\b/i;

  // Markers on a price that has been crossed out. "line-through" is the
  // Tailwind spelling and tokenises to "line through", so both forms are
  // listed -- the whole-word rule means neither can be caught by accident.
  const STALE_PRICE =
    /\b(strike|strikethrough|struck|through|crossed|old|was|mrp|orig|original|rack|discount|save|off)\b/;
  const SUPPLEMENT_CLASS = /\b(extra|addon|add on|child|person|tax|taxes|fee|fees)\b/;
  const RATE_CLASS = /\b(rate|rates|price|prices|amt|amount|tariff|cost|total)\b/;
  // Containers holding the amenity chips, badges and feature pills that every
  // card repeats. They frequently sit inside a "roomtype-*" wrapper and so
  // would otherwise collect the same bonus as the element holding the name.
  const CHROME_CLASS =
    /\b(feature|features|facility|facilities|amenity|amenities|tag|tags|chip|chips|badge|badges|icon|icons|attribute|attributes|label|labels)\b/;
  const NAME_CLASS = /\b(name|title|heading|roomtype|room type|rmname|caption)\b/;

  const pickFrom = (card) => {
    let priceEl = null, nameEl = null, nameText = null, priceValue = null;
    let bestNameScore = -1, bestPriceScore = -1, bestPriceValue = -1;
    const candidates = [];

    for (const el of card.querySelectorAll("*")) {
      if (el.children.length > 2) continue;
      const t = ownText(el);
      if (!t) continue;
      const asPrice = parsePriceStrict(t, el);

      if (asPrice !== null && t.length <= 40) {
        const where = context(el);
        let score = 0;
        if (MARKER.test(t)) score += 3;                       // says so itself
        if (RATE_CLASS.test(where)) score += 3;
        // A struck-through "was" price is visible on the page and so passes
        // verification just as well as the real one; it has to be excluded by
        // what it IS, not by whether it can be found.
        if (STALE_PRICE.test(where)) score -= 6;
        if (SUPPLEMENT_CLASS.test(where)) score -= 4;
        // What the element SAYS outranks what its classes are called, because
        // the text is the part the site cannot rename. On a real page the tax
        // line "+ ₹169.5 in taxes and charges" sat inside a wrapper called
        // "roomtype-price" and collected the same +3 as the rate itself; only
        // its own words separate the two.
        if (SUPPLEMENT.test(t) && !INCLUSIVE_TEXT.test(t)) score -= 8;
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
      // Buttons. "View Room" and "Select Room" sit inside every room card and
      // look exactly like names, so a scan can come back with two rooms both
      // called "View Room" -- confident, repeated, and useless.
      //
      // Matched only at the START of the text, because the same words appear
      // legitimately at the end of real names: "Family Room with Mountain
      // View" and "Suite with Mountain View" are two rooms at a monitored
      // property, and a blanket ban on "view" would erase both.
      if (/^(view|select|book|choose|reserve|show|see|check|explore|details?|more)\b/i.test(t)) continue;
      // Headings and small print that happen to contain a room word.
      if (/\b(per|left|available|select|choose|remaining|total|from|starting|guests?|adults?|nights?)\b/i.test(t)) continue;
      if (/\b(rates?|tax|taxes|inclusive|exclusive|prices?|policy|policies|cancellation|refundable|check-?in|check-?out|capacity)\b/i.test(t)) continue;
      const where = context(el);
      const words = t.split(/\s+/).length;
      let score = (words >= 2 ? 10 : 0) + Math.min(t.length, 40);
      if (NAME_CLASS.test(where)) score += 20;
      // A heading inside a room card is the room's name essentially always,
      // and it is the one signal here that survives a site whose classes say
      // nothing. Weighted above the length-and-word-count guess it competes
      // with, because "Suite" is a real room name that a three-word amenity
      // chip would otherwise outscore purely by being longer.
      if (/^h[1-6]$/i.test(el.tagName)) score += 25;
      // Amenity chips and feature pills, which repeat identically across every
      // card. They are often nested inside a "roomtype-*" wrapper and so
      // collect the NAME_CLASS bonus they have no claim to.
      if (CHROME_CLASS.test(where)) score -= 25;
      // Does this element carry a POSITIVE claim to be the name, as opposed to
      // merely having scored well? A heading is one by HTML semantics; a
      // "roomName"/"title" wrapper says so outright, unless it is really a
      // chip container that happens to sit under a "roomtype-*" parent.
      //
      // This is what decides whether a repeated name is trusted or replaced,
      // and it deliberately does not depend on the score: scores are a ranking
      // and this is a claim of kind.
      const isHeading = /^h[1-6]$/i.test(el.tagName);
      const trusted = isHeading || (NAME_CLASS.test(where) && !CHROME_CLASS.test(where));
      candidates.push({ el, text: t, score, trusted });
      if (score > bestNameScore) {
        bestNameScore = score;
        nameEl = el;
        nameText = t;
      }
    }
    // Ranked best-first so a winner that turns out to read the same on every
    // card can be replaced by the next one down. See resolveNameSelector.
    candidates.sort((a, b) => b.score - a.score);
    return { priceEl, nameEl, nameText, priceValue, candidates };
  };

  // A class-based selector when the classes are real; a text-based one when
  // they are generated hashes. The second is not a lesser option here -- on a
  // styled-components site it is the only one that will still work after the
  // next deploy.
  const usable = (s) => s && (s.includes(".") || s.includes("[") || s.startsWith("text="));

  // Playwright's "text=" selectors are not CSS and cannot be handed to
  // querySelector, so a card is read through one only by falling back to the
  // per-card pick that produced it.
  const readName = (card, selector, found) => {
    if (selector && !selector.startsWith("text=")) {
      let hit = null;
      try { hit = card.querySelector(selector); } catch (e) { hit = null; }
      return hit ? ownText(hit) || clean(hit.textContent).trim() : null;
    }
    return found.nameText;
  };

  // A name that reads the same on every card is USUALLY not a name.
  //
  // On one real site "King Size Bed" sits in the amenity chips of all six
  // cards and outscored the <h5> holding the actual room name. The fetch then
  // returned six offers sharing one name, they all resolved to a single room
  // type, and five were discarded downstream as duplicate offer keys -- a
  // six-room hotel displayed as one room, priced at its own tax line.
  //
  // No amount of class-name scoring can be trusted to prevent that on every
  // site, so the check here is behavioural rather than cosmetic: read every
  // card through the candidate SELECTOR, and if it says the same thing in all
  // of them, treat the winner as suspect. The selector is what gets tested,
  // not the element, because the selector is the artefact that is stored and
  // re-run on every future check.
  //
  // "USUALLY", because repetition is not proof. A property with ONE room type
  // and three rate plans -- room only, breakfast, half board -- lists three
  // cards that genuinely all say "Deluxe Room", and an earlier version of this
  // function duly rejected the correct <h3> and named the rooms after their
  // board basis instead. That is the same silent wrongness in the other
  // direction.
  //
  // So repetition alone does not disqualify a candidate: it disqualifies an
  // UNTRUSTED one. A heading, or an element in a container that calls itself a
  // name, is allowed to repeat -- rate plans are a real thing and this is what
  // they look like. An amenity chip is not, and gets replaced. `trusted` is a
  // claim of kind rather than a score, which is why the two cases separate
  // cleanly instead of trading off against each other.
  const resolveNameSelector = (cards, candidates) => {
    let fallback = null;
    for (const cand of candidates || []) {
      let sel = selectorFor(cand.el);
      if (!usable(sel)) sel = textSelectorFor(cand.el);
      if (!usable(sel)) continue;

      // Text selectors are built from ONE card's wording and cannot be
      // evaluated across the others from here. They are kept as a last resort
      // -- on a hashed-class site they are the only thing that works -- but
      // never preferred over a CSS selector proven to vary.
      if (sel.startsWith("text=")) {
        if (!fallback) {
          fallback = { selector: sel, sampleText: cand.text, distinct: 0,
                       trusted: !!cand.trusted };
        }
        continue;
      }

      const seen = [];
      for (const card of cards) {
        const value = readName(card, sel, cand);
        if (value) seen.push(value);
      }
      // Must actually find a name in most cards: a selector that resolves in
      // the sample and nowhere else is worse than the one it replaced.
      if (seen.length * 2 < cards.length) continue;
      const distinct = new Set(seen).size;
      const result = { selector: sel, sampleText: seen[0] || cand.text,
                       distinct: distinct, trusted: !!cand.trusted };

      // Varies across the cards, or there is only one card for it to vary
      // against. Nothing to doubt.
      if (distinct > 1 || cards.length < 2) return result;

      // Reads identically everywhere. A heading or a self-declared name
      // container is taken at its word -- one room type sold on three rate
      // plans really does repeat -- and the repetition is reported so the
      // pipeline downstream can still act on it.
      if (cand.trusted) return result;

      // Otherwise it is a shared label wearing a room name's clothes. Keep
      // looking, and remember this one so a page where every candidate
      // collapses still returns something rather than vanishing: the honest
      // `distinct` and `trusted` values travel with it, and Python refuses to
      // store the combination.
      if (!fallback) fallback = result;
    }
    return fallback;
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
    const withPrice = nodes.filter(n => parsePriceStrict(clean(n.textContent), n) !== null);
    if (!withPrice.length) continue;

    const sample = withPrice[0];
    const picked = pickFrom(sample);
    let priceEl = picked.priceEl, nameEl = picked.nameEl, nameText = picked.nameText;
    if (!priceEl || !nameEl) continue;

    let priceSel = selectorFor(priceEl);
    if (!usable(priceSel)) priceSel = textSelectorFor(priceEl);
    if (!usable(priceSel)) continue;

    const sampled = withPrice.slice(0, 10);
    const resolved = resolveNameSelector(sampled, picked.candidates);
    if (!resolved) continue;
    const nameSel = resolved.selector;
    nameText = resolved.sampleText || nameText;

    // A room's name and its price are never the same element.
    //
    // When both resolve to one selector the scan has not found a room card.
    // It has found a repeated pair of sibling elements and read one of them
    // twice -- and because the two reads agree, every check downstream
    // agrees with them. A real hotel was configured with
    // "div.vres-chk-box > span" as BOTH its room_name and its price, which
    // is the amenity filter sidebar: "Air conditioning", "Show Only
    // Available Rooms". Corroboration passed, because the number the scan
    // called a price was a room size printed on the page it was checked
    // against, and the repair wrote itself into the database as "1 rooms,
    // 1/1 prices confirmed".
    //
    // Nothing further downstream can catch this, because from there the
    // candidate is indistinguishable from a correct one. It has to die here.
    if (nameSel === priceSel) continue;

    // Each card is read on its own terms. Resolving the SAMPLE's elements for
    // every card reported one room's price three times -- three rows, three
    // identical numbers, and a verification step with nothing to catch.
    //
    // Names come from the agreed selector rather than from each card's own
    // best guess, so what is reported here is what the adapter will actually
    // read back later. Prices stay per-card: they are legitimately allowed to
    // repeat, so there is nothing for a cross-card check to add.
    const names = [], prices = [];
    for (const card of sampled) {
      const found = pickFrom(card);
      const n = readName(card, nameSel, found);
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
      // Rooms in a list have DIFFERENT names. Two cards reporting the same
      // name is the signature of a selector that found a shared label rather
      // than the room, and it outranks nothing once counted.
      distinct: new Set(names).size,
      // ...unless the element making that claim is a heading or a container
      // that calls itself a name, in which case one room type on several rate
      // plans is the likelier reading. Python needs both facts to tell a
      // legitimate repeat from a broken selector, so both are reported rather
      // than resolved into a verdict here.
      name_trusted: !!resolved.trusted,
    });
  }

  // Most rooms first; then the candidate whose names actually differ, which
  // separates a room list from a column of identical buttons; then the
  // tightest card, because a container holding one room beats one holding the
  // whole page; and only then a stable anchor over a class list.
  cards.sort((a, b) =>
    (b.matched - a.matched) || (b.distinct - a.distinct) ||
    (a.cardLen - b.cardLen) || (b.anchored - a.anchored) || (b.count - a.count));
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
