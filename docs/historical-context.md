# Historical XRP/Ripple Context

The intelligence extractor receives two deliberately separate inputs:

- **Current article evidence**: the only permitted evidence for claims about what the current source reports.
- **Curated historical background**: source-linked records used to recognize old relationships and interpret why a new development may matter.

The historical pack is `config/historical_context.json`. It is not a 180-day feed cache. It has no rolling expiration and is selected by entity aliases in the current title and summary. At most four matching records are added to a model prompt.

## Rules that must remain true

1. A historical partnership, pilot, customer, or product does not prove an active relationship today.
2. Ripple network adoption is not automatically XRP usage. The pack explicitly labels relationships where XRP usage was not established by the cited source.
3. Ripple (the company), XRP (the asset), and XRPL/XRP Ledger (the network) are distinct.
4. Claims must cite only the current article/source evidence. Historical background can inform significance, not act as evidence for current claims.
5. Ended relationships must remain marked as ended. For example, the MoneyGram record distinguishes the February 2021 suspension, the March 8, 2021 end-of-partnership announcement, and Ripple's March 9-dated statement page; both companies said they were committed to revisiting the relationship.
6. Every record needs a date, classification, status caveat, and HTTPS source URL. Prefer primary sources, and add a counter-record when later evidence materially changes a relationship's status.

## Adding or updating a record

- Find the original announcement and, where possible, the counterparty's statement.
- Record what was actually announced: trial, RippleNet integration, XRP/ODL use, investment, custody, stablecoin, XRPL deployment, or something else.
- Keep dates and status explicit. Do not silently convert "exploring" into "launched", or a historical customer into a current customer.
- Add useful aliases for retrieval, but avoid generic aliases that cause unrelated stories to match.
- Add/update tests for the relationship and its status.
- Review context selection and prompt output before allowing any intelligence publication.

This is a curated foundation, not an exhaustive history. Expansion should be research-driven, prioritized by relationships likely to help connect new bank, payments, regulatory, stablecoin, custody, tokenization, and XRPL developments.


## Current coverage added in the institutional-history expansion

The pack records source-linked snapshots for:

- SBI Remit, Coins.ph, and SBI VC Trade's 2021 Japan-to-Philippines ODL launch, where Ripple explicitly named XRP.
- RLUSD's December 2024 launch, kept distinct from XRP.
- Ripple Payments' 2025 RLUSD integration and the select customers named in that announcement.
- BNY's announced primary custody role for RLUSD reserves.
- Ripple's 2025 Hidden Road acquisition completion and the resulting Ripple Prime business, with RLUSD collateral and XRPL migration claims kept distinct.
- Ripple's announced GTreasury acquisition and the 2026 launch of native digital-asset capabilities in Ripple Treasury.
- The SEC's August 2025 dismissal of its appeal and Ripple's cross-appeal, while preserving the final judgment's penalty and injunction caveats.
- Ripple Prime's October 2026 expanded relationship with Brevan Howard.
- Ripple's July 2026 full MiCA CASP authorization, distinguished from product-specific availability and XRP adoption.
- The DBS / Franklin Templeton / Ripple MoU involving tokenized sgBENJI and RLUSD on/around XRPL.
- The Securitize integration announcement involving RLUSD as an exchange option for tokenized BUIDL and VBILL fund shares.

These records deliberately separate XRP bridge-asset use from RLUSD stablecoin use, XRPL tokenization, and custody relationships. An announcement or MoU is not automatically evidence that every planned feature launched or remains active.
