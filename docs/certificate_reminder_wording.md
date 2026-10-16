# Breeder certificate reminders: revised wording (16 Oct 2026, for Saevon's approval, then Ben)

The design (banner, card, footer) is unchanged: it's the AQUA template Ben already uses. Only the words change.

The previews in `preview/` render all seven with the sample data.

## Applies to every reminder

- Greeting: **Hi {name},**. Sign-off: **Thank you, The AQUA AI team**. Use "AQUA AI" throughout, not "Aqua AI".
- Upload line, for the button-less action paragraph: **Upload your certificate in the Aqua Providers app, under your account settings.**
- Help line, every email: **If you need any help, just reply to this email or contact support@aquaai.uk.** (Reply-To is already support@aquaai.uk.)
- Details table: rename "Licence number" to **Certificate number**. When there's no expiry date, show **No expiry date shown**.
- Small grey note: **If you've already uploaded your renewed certificate, please ignore this email.** (Email 1 has its own note.)

## The seven

| # | Stage | Subject | Badge | Opening line | Box | After the upload line |
|---|---|---|---|---|---|---|
| 1 | No certificate (7 days after sign-up, then monthly) | Upload your certificate to offer delivery to your customers | Certificate needed | We don't yet have a breeder certificate on file for your AQUA AI account. | You can still list your stock and let customers collect in person. A valid certificate also lets you offer delivery. *Then a line under the box:* Selling animals as pets as a business usually needs a licence. To find out how to get one, see our guide at aquaai.uk/faqs (link to the FAQ's breeder-licence section; draft in FAQ_Breeder_Licence_Draft.md). | We'll review it and switch delivery on for you once it's approved. *(note: If you only sell for collection in person, there's nothing you need to do.)* |
| 2 | Expires within 30 days | Your certificate is due for renewal | Renewal due | This is a friendly reminder that your breeder certificate expires on **{date}** (in {n} days). | Your certificate is what lets you offer delivery to your customers through AQUA AI. If it expires, you can still list your stock and sell for collection, but delivery will be paused until a renewed certificate is approved. | We'll review it and keep delivery running for your business. |
| 3 | Expires within 7 days | Your certificate expires in {n} days | Expires soon | Your breeder certificate expires on **{date}**, in {n} days. | To keep offering delivery to your customers without a break, please upload your renewed certificate before then. After it expires you can still list your stock and sell for collection, but delivery will be paused until the new certificate is approved. | We'll review it as quickly as we can. |
| 4 | Expired | Your certificate has expired, so delivery is paused | Delivery paused (red) | Your breeder certificate expired on **{date}**, so delivery from your AQUA AI store is paused for now. | Your listings stay live, and customers can still buy from you for collection in person. | We'll review it and switch delivery back on as soon as it's approved. |
| 5 | No expiry date, over 1 year old | Time to plan your certificate renewal | Renewal reminder (blue) | Your breeder certificate was issued on **{issued}**. As it doesn't show an expiry date, we ask for a renewed certificate every two years, so please plan to upload a new one by **{due}**. | Nothing changes in the meantime: your listings, collection and delivery all continue as normal. | — |
| 6 | No expiry date, over 2 years old | Your certificate is due for renewal | Renewal due | Your breeder certificate was issued on **{issued}**, more than two years ago, so it's now due for renewal. | Please upload your current certificate so your verification stays up to date. Your listings, collection and delivery continue as normal in the meantime. | — |
| 7 | No dates at all (yearly) | Please confirm your certificate is still current | Confirmation needed (blue) | The breeder certificate on your AQUA AI account doesn't show an issue or expiry date, so we ask you to confirm once a year that it's still current. | Please upload your most recent certificate. Your listings, collection and delivery continue as normal in the meantime. | — |

## Before these go live

1. **Emails 1–4 describe the new rule:** list and sell for collection without a certificate; only delivery needs one. Today's backend still blocks an unlicensed breeder from everything: listing visibility, collection and checkout (`collection_enabled_effective`, `bulk_visibility_update`, the daily sweep closing listings). **Turn these reminders on in the same release as prerequisite P1 of the V2.2 spec**, or they tell breeders something that isn't true.
2. **AQUA's knowledge must match.** IAMO's entry "Provider certificate and licence renewals" still says a certificate with no expiry date is due once it's a year old. With Ben's two-year cycle, CC updates that entry when these reminders go live.
