# Room price feed — API contract (version 1)

The hotel price monitor reads **tonight's room prices for our own hotel** from
Booking.com, Agoda and Goibibo, exactly as each platform shows them to a guest.
This document is everything another system needs to receive those prices — it
was written for Sales Daddy, whose WhatsApp assistant quotes room prices from
its product list, but nothing in it is specific to Sales Daddy.

There are two ways to get the data. They carry **the same JSON**:

| | Who calls whom | When |
|---|---|---|
| **A. Push** | we call **your** endpoint | whenever a price changes, and at least every 6 hours |
| **B. Pull** | **you** call our endpoint | whenever you like (testing, catching up after downtime, or instead of A) |

Use A for live prices, and B to build and test against before A is switched on.

---

## The data

One **list** per hotel per night. One **item** per room per platform.

```json
{
  "version": 1,
  "source": "rms",
  "hotel_id": 9,
  "hotel": "ASG HOLIDAY RESORTS",
  "night": "2026-10-29",
  "complete": true,
  "items": [
    {
      "external_id": "h9-r25-goibibo",
      "name": "Classic Room – Goibibo – tonight",
      "room": "Classic Room",
      "channel": "Goibibo",
      "available": true,
      "price": 4475,
      "price_with_tax": 5001,
      "meal_plan": "room only",
      "currency": "INR",
      "description": "Tonight on Goibibo, 2 adults, room only: ₹5,001 including taxes.",
      "seen_at": "2026-10-29T08:52:58.123456+00:00"
    },
    {
      "external_id": "h9-r12-www-booking-com",
      "name": "Deluxe Double Room – Booking.com – tonight",
      "room": "Deluxe Double Room",
      "channel": "Booking.com",
      "available": false,
      "price": null,
      "price_with_tax": null,
      "meal_plan": null,
      "currency": "INR",
      "description": "Sold out tonight on Booking.com.",
      "seen_at": "2026-10-29T09:01:12.000000+00:00"
    }
  ]
}
```

### List fields

| Field | Type | Meaning |
|---|---|---|
| `version` | int | Contract version. Only goes up if a field **changes meaning or is removed**. New fields may be added at any time without a bump — ignore fields you do not know. |
| `source` | string | Always `"rms"`. Together with `external_id` it identifies a product. |
| `hotel_id` | int | Our id for the hotel. Stable. |
| `hotel` | string | The hotel's name. |
| `night` | date `YYYY-MM-DD` | The check-in night these prices are for (one night, 2 adults). In India time (Asia/Kolkata). |
| `complete` | bool | `true` = these are **all** of this hotel's items. An `external_id` you stored earlier for this `hotel_id` that is **not** in this list no longer exists (e.g. the platform renamed the room) — stop quoting it. |
| `items` | array | The products, below. |

### Item fields

| Field | Type | Meaning |
|---|---|---|
| `external_id` | string | **The upsert key.** Stable for a room on a platform: `h<hotel>-r<room>-<platform>`. Treat as opaque. |
| `name` | string | Ready-made product name: room – platform – tonight. |
| `room` | string | The room's name. |
| `channel` | string | The platform: `Booking.com`, `Agoda`, `Goibibo` (others may appear). |
| `available` | bool | `false` = **sold out tonight on this platform**. Do not quote a price. |
| `price` | int or null | Rupees, the platform's headline price (usually before tax). `null` when sold out. |
| `price_with_tax` | int or null | Rupees, all-in price including taxes, **when the platform shows it**. `null` when sold out or not shown. **Quote this one when present.** |
| `meal_plan` | string or null | The board of the price quoted (`room only`, `breakfast`, …). `null` = not stated. |
| `currency` | string | ISO code, `INR`. |
| `description` | string | One sentence a chat assistant can repeat as-is. |
| `seen_at` | ISO 8601 timestamp | When this price was last read off the platform. |

**Which price per room:** a room is sold under several boards and
cancellation terms. The item carries the **cheapest one on sale** — what a
guest asking "how much is the Deluxe?" expects to hear.

---

## A. Push — we call you

```
PUT <your URL>
Content-Type: application/json
X-Api-Key: <the key you gave us>

<one list, as above>
```

* **One hotel per call.** A deployment with two hotels makes two calls.
* **Upsert** every item on `source` + `external_id`. Sending the same list
  twice must change nothing.
* Apply `complete: true` as described above.
* **When we call:** after a platform check of the hotel finds a different
  list from the one we last sent, and at least every **6 hours** even if
  nothing changed — so if `seen_at` on your side gets older than about a day,
  something is wrong and you should stop quoting the price.
* **Your answers:**

  | You answer | We do |
  |---|---|
  | `2xx` | done |
  | `429` or `5xx`, or no answer within ~20 s | retry, backing off from 30 s to 10 min, up to 5 times; the next price change sends again anyway |
  | any other `4xx` (bad key, body you reject) | log it and do not retry; fix and the next change sends again |

To switch the push on, our side sets two values in Coolify and redeploys:

* `PRICE_FEED_PUSH_URL` = your full URL (default
  `https://api.salesdaddy.in/v1/ext/products`)
* `PRICE_FEED_PUSH_KEY` = the key you gave us, sent as `X-Api-Key`

Until the key is set, nothing is sent. These settings are separate from the
ones that send our WhatsApp alerts, so changing them cannot affect those.

## B. Pull — you call us

```
GET https://hpm.103.178.166.6.sslip.io/api/v1/price-feed
X-Api-Key: <the key we give you>
```

Optional: `?hotel_id=9` for one hotel.

Answer `200`:

```json
{
  "version": 1,
  "night": "2026-10-29",
  "hotels": [ <one list per hotel, exactly as above> ]
}
```

| Status | Meaning |
|---|---|
| `200` | the lists |
| `401` | missing or wrong `X-Api-Key` |
| `404` | the feed is not switched on here, or no such `hotel_id` |

Always tonight's data, read fresh from our database on every call — calling
every few minutes is fine. To switch it on, our side sets
`PRICE_FEED_API_KEY` (in Coolify) to a long random secret and shares it with you.

---

## Suggested mapping onto a product list

For a receiver like Sales Daddy's `products` table:

| Product column | From |
|---|---|
| `source` | `"rms"` (or a namespaced value of your own) |
| `external_id` | `external_id` |
| `name` | `name` |
| `category` | `"Rooms – " + channel` |
| price | `price_with_tax` if not null, else `price` (rupees — multiply by 100 for paise); tax **inclusive** when `price_with_tax` was used |
| in stock / quotable | `available` |
| description | `description` |
| last synced | `seen_at` |

Only rows with your `source` value should ever be written by this feed;
products a person made by hand must never be changed by it.
