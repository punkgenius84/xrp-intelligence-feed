# Historical XRP/Ripple Context

The intelligence extractor receives two deliberately separate inputs:

- **Current article evidence**: the only permitted evidence for claims about what the current source reports.
- **Curated historical background**: source-linked records used to recognize old relationships and interpret why a new development may matter.

The historical pack is `config/historical_context.json`. It is not a 180-day feed cache. It has no rolling expiration and is selected by entity aliases in the current title and summary. At most six matching records are added to a model prompt. Selection ranks specific product/program matches above generic mentions of XRP or Ripple, uses curator priority and recency, and adds a small topic-diversity preference so older generic history does not crowd out directly relevant newer developments.

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

This is a curated foundation, not an exhaustive history. Expansion is research-driven and includes both commercial milestones and XRP/XRPL protocol mechanics. Each entry carries a topic and curator priority to improve retrieval; newer facts and dynamic metrics must be rechecked against current primary sources.


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


## Fixed intelligence headline evaluation

The fixed regression/evaluation set is `config/intelligence_evaluation.json` and contains ten sourced examples spanning an ended relationship, historical XRP/ODL use, RLUSD, custody, tokenization, acquisitions, the SEC case, treasury capabilities, and institutional prime brokerage.

Run the full set against the configured local Ollama model from the repository root:

```powershell
python scripts/evaluate_intelligence.py
```

Select one case with `--case <id>`, or override the installed model with `--model <ollama-model>`. The runner checks expected event types and required/forbidden text patterns against the model's validated analysis. It is a repeatable quality screen, not a proof of correctness: inspect the full model output and source material, update expectations only when primary-source evidence supports the change, and keep `INTELLIGENCE_PUBLISH=false` until scheduled shadow output has been reviewed.


## XRP and XRPL mechanics added in the long-term context expansion

The expanded pack now includes source-linked reference records for:

- XRP as XRPL's native asset, the 2012 launch, validator consensus, and the distinction between Ripple, XRP, and the ledger.
- XRP transaction-cost destruction versus XRP held in account/object reserves, including validator fee voting and the need to recheck current parameters.
- XRPL's native DEX and conditional auto-bridging through XRP when that route is more competitive.
- XLS-30 AMM activation, LP tokens and trading-fee mechanics, asset-pair composition, and liquidity-provider exchange-rate risk.
- The original 55-billion-XRP escrow schedule, including the difference between gross releases, re-escrows, liquid balances, and net circulating-supply changes.
- ODL's explicitly documented XRP bridge-asset mechanism, kept distinct from Ripple Payments flows that use RLUSD or other settlement methods.
- RLUSD as an issued stablecoin on XRPL and other networks, distinct from native XRP.
- Ripple National Trust Bank's OCC-recorded effective date and trust-bank scope; BNY's announced support for XRP and RLUSD custody; and C2FO's announced XRPL trade-finance initiative that specifies RLUSD settlement.

The persistent thesis tracker also adds separate XRP-specific themes for confirmed ODL/bridge-asset use, XRPL auto-bridging, XRPL AMM liquidity, and XRP escrow/supply events. These themes require an evidence-linked reported-fact claim containing the relevant XRP/XRPL mechanism; significance-only model speculation cannot open them. Thesis evidence still requires multiple distinct validated events and multiple independent source IDs.
