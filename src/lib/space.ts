export type Astronaut = {
  id: string
  name: string
  gender: string | null
  nationality: string | null
  wikipedia_url: string | null
  wikidata_id: string | null
  first_flight_rank: number | null
  first_flight_date: string | null
  first_flight_unix: number | null
  first_flight_name: string | null
  first_flight_year: number | null
  time_in_space_seconds: number | null
  segment_count: number
  is_currently_in_space: number
  mission_count: number
  image_url?: string | null
}

export type Mission = {
  id: string
  name: string
  wikipedia_url: string | null
  launch_at: string | null
  launch_unix: number | null
  landing_at: string | null
  landing_unix: number | null
  duration_seconds: number | null
}

export type Interval = {
  id: number
  astronaut_id: string
  mission_id: string
  mission_name: string
  mission_url: string | null
  launch_at: string | null
  launch_unix: number | null
  landing_at: string | null
  landing_unix: number | null
  duration_seconds: number | null
  is_current: number
}

export type Segment = {
  id: number
  astronaut_id: string
  segment_index: number
  start_at: string
  start_unix: number
  end_at: string | null
  end_unix: number | null
  duration_seconds: number | null
  is_current: number
  source_interval_ids: string
  mission_ids: string[]
  mission_names: string[]
  year: number | null
}

export type SpaceData = {
  generated_at: string
  snapshot_unix: number
  stats: {
    astronauts: number
    missions: number
    segments: number
    current: number
    countries: number
    total_time_seconds: number
  }
  people: Astronaut[]
  missions: Mission[]
  intervals: Interval[]
  segments: Segment[]
}

export function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("")
}

export function days(seconds: number | null | undefined, digits = 0) {
  if (!seconds) return "0"
  return (seconds / 86400).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  })
}

export function totalTime(seconds: number | null | undefined) {
  if (!seconds) return "0 days"
  const totalDays = seconds / 86400
  if (totalDays >= 365) {
    return `${(totalDays / 365.2425).toLocaleString(undefined, {
      maximumFractionDigits: 1,
    })} years`
  }
  if (totalDays >= 1) {
    return `${totalDays.toLocaleString(undefined, {
      maximumFractionDigits: 1,
    })} days`
  }
  return `${Math.round(seconds / 60).toLocaleString()} minutes`
}

export function compactDate(iso: string | null | undefined) {
  if (!iso) return "Present"
  const date = new Date(iso)
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date)
}

export function yearFromUnix(value: number | null | undefined) {
  if (!value) return null
  return new Date(value * 1000).getUTCFullYear()
}

export type Region = "United States" | "Soviet Union / Russia" | "China" | "Europe" | "Other"

const europe = [
  "Austria",
  "Belgium",
  "Bulgaria",
  "Czechoslovakia",
  "Denmark",
  "France",
  "Germany",
  "Hungary",
  "Italy",
  "Netherlands",
  "Poland",
  "Romania",
  "Slovakia",
  "Spain",
  "Sweden",
  "Switzerland",
  "United Kingdom",
]

export function regionFor(nationality: string | null | undefined): Region {
  const text = nationality ?? ""
  if (text.includes("United States")) return "United States"
  if (text.includes("Russia") || text.includes("Soviet Union")) return "Soviet Union / Russia"
  if (text.includes("China")) return "China"
  if (europe.some((country) => text.includes(country))) return "Europe"
  return "Other"
}

export const regionColors: Record<Region, string> = {
  "United States": "#5b7cfa",
  "Soviet Union / Russia": "#a855f7",
  China: "#f97345",
  Europe: "#22a6a6",
  Other: "#8892a6",
}

export function hash(input: string) {
  let value = 0
  for (let index = 0; index < input.length; index += 1) {
    value = (value * 31 + input.charCodeAt(index)) >>> 0
  }
  return value
}
