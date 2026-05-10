# Sample conversation: alternate_factor_recovery

_ACC1001 user fails verification twice across different secondary factors before recovering. First attempt: ambiguous DOB resolved to wrong reading (April 5 vs on-file May 14) → fails. Second attempt: switches to pincode 110091 → fails (wrong pincode). Third attempt: provides correct DOB 1990-05-14 → succeeds. Demonstrates DECISIONS #2 field-level transparent failure (the user knows which factor failed and what the alternates are) + DECISIONS #3 per-cycle retry budget (cap=3, 2 fails consumed, 3rd attempt succeeds within budget) + the v2 disambiguation flow as the entry point._

---

**User:** _(session start; agent greets)_

**Agent:** Hello! I'm here to help you make a payment. Could you share your account ID to get started?

**User:** ACC1001

**Agent:** I've found your account. To verify your identity, could you share your full name along with one of: date of birth, last 4 digits of Aadhaar, or pincode?

**User:** Nithin Jain, DOB 05-04-1990

**Agent:** That date is ambiguous — could it be April 5, 1990 or May 4, 1990? Please reply with the correct date (e.g., 'April 5, 1990').

**User:** April 5, 1990

**Agent:** That date of birth doesn't match. You can try again, or share a different secondary factor — Aadhaar last 4 or pincode.

**User:** 110091

**Agent:** That pincode doesn't match. You can try again, or share a different secondary factor — date of birth or Aadhaar last 4.

**User:** 1990-05-14

**Agent:** Thanks, Nithin! Your identity is verified. Your outstanding balance is ₹1,250.75. How much would you like to pay?

**User:** 500

**Agent:** Got it — ₹500. Now I need your card details: card number, CVV, and expiry (MM/YY or MM/YYYY).

**User:** Card 4532 0151 1283 0366, CVV 123, expires 12/27

**Agent:** I'm about to charge ₹500 to card ending 0366, expiry 12/2027. Reply 'yes' to confirm.

**User:** yes

**Agent:** Payment successful. Account: ACC1001. Amount paid: ₹500. Transaction ID: txn_1778435978381_qaa6htk. Card ending: 0366. Remaining balance: ₹750.75.

---

**Final stage:** `terminal_completed`
