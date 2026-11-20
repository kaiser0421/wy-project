const currency = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "TWD", currencyDisplay: "symbol", maximumFractionDigits: 0,
});

export const money = (amount: number) => currency.format(amount);

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
export const date = (value: string) => Number.isFinite(Date.parse(value)) ? dateTime.format(new Date(value)) : "—";
export const label = (value: string) => value.replaceAll("_", " ").replace(/^./, character => character.toUpperCase());

export function countdown(expiresAt: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
  if (!Number.isFinite(seconds)) return "—";
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
