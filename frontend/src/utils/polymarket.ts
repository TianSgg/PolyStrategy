export function polymarketEventSlug(
  eventSlug: string | null | undefined,
): string | null {
  const explicit = eventSlug?.trim()
  return explicit || null
}
