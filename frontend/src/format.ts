const currency = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "TWD", currencyDisplay: "symbol", maximumFractionDigits: 0,
});

export const money = (amount: number) => currency.format(amount);

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
export const date = (value: string) => Number.isFinite(Date.parse(value)) ? dateTime.format(new Date(value)) : "—";
const clockTime = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hourCycle: "h23",
});
const shortDateTime = new Intl.DateTimeFormat(undefined, {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
/** Compact month/day and time for dense tables (the full date stays available on hover). */
export const shortDate = (value: string) => Number.isFinite(Date.parse(value)) ? shortDateTime.format(new Date(value)) : "—";
/** Compact time of day for log tables (the full date stays available on hover). */
export const clock = (value: string) => Number.isFinite(Date.parse(value)) ? clockTime.format(new Date(value)) : "—";
export const label = (value: string) => value.replaceAll("_", " ").replace(/^./, character => character.toUpperCase());

export function countdown(expiresAt: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
  if (!Number.isFinite(seconds)) return "—";
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
