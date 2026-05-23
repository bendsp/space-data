import { useEffect, useMemo, useState } from "react"
import type { CSSProperties } from "react"
import { ArrowUpRight, Orbit, Search } from "lucide-react"

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import {
  compactDate,
  days,
  hash,
  initials,
  regionColors,
  regionFor,
  totalTime,
  yearFromUnix,
  type Astronaut,
  type Region,
  type Segment,
  type SpaceData,
} from "@/lib/space"
import "./app.css"

const startYear = 1960
const endYear = 2030
const tickYears = Array.from({ length: 8 }, (_, index) => startYear + index * 10)
const visibleRegions: Region[] = [
  "United States",
  "Soviet Union / Russia",
  "Europe",
  "China",
  "Other",
]

function HeroStat({ value, label }: { value: string; label: string }) {
  return (
    <div className="hero-stat">
      <span>{value}</span>
      <small>{label}</small>
    </div>
  )
}

function PersonAvatar({ person, className }: { person: Astronaut; className?: string }) {
  return (
    <Avatar className={className} size="lg">
      {person.image_url ? <AvatarImage src={person.image_url} alt={person.name} /> : null}
      <AvatarFallback>{initials(person.name)}</AvatarFallback>
    </Avatar>
  )
}

function TimelineDot({
  person,
  x,
  y,
  size,
  selected,
  onSelect,
}: {
  person: Astronaut
  x: number
  y: number
  size: number
  selected: boolean
  onSelect: (person: Astronaut) => void
}) {
  const region = regionFor(person.nationality)
  const color = regionColors[region]
  const style = {
    "--x": `${x}%`,
    "--y": `${y}px`,
    "--size": `${size}rem`,
    "--dot": color,
    zIndex: Math.round(size * 1000),
  } as CSSProperties
  const align = x < 12 ? "start" : x > 88 ? "end" : "center"
  const tooltipId = `timeline-card-${person.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`

  return (
    <div className="timeline-dot-wrap" style={style} data-align={align} data-selected={selected}>
      <button
        className="timeline-dot"
        type="button"
        aria-label={person.name}
        aria-describedby={tooltipId}
        data-selected={selected}
        onClick={() => onSelect(person)}
      />
      <div className="timeline-hover" id={tooltipId} role="tooltip">
        <strong>{person.name}</strong>
        <span>{person.first_flight_name}</span>
        <div className="hover-meta">
          <span>{person.first_flight_date}</span>
          <span>{totalTime(person.time_in_space_seconds)}</span>
        </div>
      </div>
    </div>
  )
}

function Timeline({
  people,
  selected,
  onSelect,
}: {
  people: Astronaut[]
  selected: Astronaut
  onSelect: (person: Astronaut) => void
}) {
  const dots = useMemo(
    () =>
      people
        .filter((person) => person.first_flight_unix)
        .map((person) => {
          const year = yearFromUnix(person.first_flight_unix) ?? startYear
          const dayOffset = new Date((person.first_flight_unix ?? 0) * 1000).getUTCMonth() / 12
          const x = ((year + dayOffset - startYear) / (endYear - startYear)) * 100
          const y = 30 + (hash(person.id) % 9) * 11
          const timeDays = (person.time_in_space_seconds ?? 0) / 86400
          const size = 0.42 + Math.min(0.42, Math.log10(timeDays + 1) * 0.13)
          return { person, x: Math.max(0, Math.min(100, x)), y, size }
        }),
    [people],
  )

  return (
    <section className="timeline-section" id="timeline">
      <div className="section-heading">
        <div>
          <h2>Timeline</h2>
          <p>{people.length.toLocaleString()} people, ordered by first flight.</p>
        </div>
      </div>

      <div className="timeline-axis" aria-hidden="true">
        {tickYears.map((year) => (
          <span
            key={year}
            style={{ left: `${((year - startYear) / (endYear - startYear)) * 100}%` }}
          >
            {year}
          </span>
        ))}
      </div>

      <div className="timeline-field">
        {dots.map(({ person, x, y, size }) => (
          <TimelineDot
            key={person.id}
            person={person}
            x={x}
            y={y}
            size={size}
            selected={person.id === selected.id}
            onSelect={onSelect}
          />
        ))}
      </div>

      <div className="timeline-legend">
        {visibleRegions.map((region) => (
          <span key={region}>
            <i style={{ background: regionColors[region] }} />
            {region}
          </span>
        ))}
      </div>
    </section>
  )
}

function CurrentSpace({
  people,
  segmentsByPerson,
  nowUnix,
  onSelect,
}: {
  people: Astronaut[]
  segmentsByPerson: Map<string, Segment[]>
  nowUnix: number
  onSelect: (person: Astronaut) => void
}) {
  const assignments = people
    .map((person) => {
      const segment = segmentsByPerson.get(person.id)?.find((item) => item.is_current)
      return segment ? { person, segment } : null
    })
    .filter((item): item is { person: Astronaut; segment: Segment } => Boolean(item))
    .sort((a, b) => a.segment.start_unix - b.segment.start_unix || a.person.name.localeCompare(b.person.name))

  if (!assignments.length) return null

  return (
    <section className="current-section" aria-labelledby="current-heading">
      <div className="section-heading current-heading">
        <div>
          <h2 id="current-heading">In Space Now</h2>
          <p>{assignments.length.toLocaleString()} people currently beyond Earth.</p>
        </div>
      </div>

      <div className="current-list">
        {assignments.map(({ person, segment }) => {
          const elapsed = Math.max(0, nowUnix - segment.start_unix)
          return (
            <button
              className="current-person"
              key={person.id}
              type="button"
              onClick={() => onSelect(person)}
            >
              <PersonAvatar person={person} className="current-avatar" />
              <span className="current-name">{person.name}</span>
              <span className="current-mission">{segment.mission_names.slice(0, 2).join(" / ")}</span>
              <span className="current-time">{days(elapsed)}d</span>
            </button>
          )
        })}
      </div>
    </section>
  )
}

function DetailPanel({
  person,
  segments,
}: {
  person: Astronaut
  segments: Segment[]
}) {
  return (
    <aside className="detail-panel" id="astronaut">
      <div className="person-head">
        <PersonAvatar person={person} className="person-avatar" />
        <div>
          <h2>{person.name}</h2>
          <p>{person.nationality ?? "Nationality unknown"}</p>
        </div>
      </div>

      <div className="detail-metrics" aria-label={`${person.name} summary`}>
        <div>
          <strong>{person.segment_count}</strong>
          <span>Flights</span>
        </div>
        <div>
          <strong>{days(person.time_in_space_seconds, person.time_in_space_seconds && person.time_in_space_seconds < 86400 ? 2 : 0)}</strong>
          <span>Days</span>
        </div>
        <div>
          <strong>{person.first_flight_year ?? "—"}</strong>
          <span>First</span>
        </div>
      </div>

      <Separator />

      <div className="mission-list-head">
        <h3>Flights</h3>
      </div>

      <ScrollArea className="mission-scroll">
        <div className="mission-list">
          {segments.map((segment) => (
            <div className="mission-row" key={segment.id}>
              <div className="mission-year">{segment.year}</div>
              <div>
                <strong>{segment.mission_names.slice(0, 2).join(" / ")}</strong>
                <span>
                  {compactDate(segment.start_at)} – {compactDate(segment.end_at)}
                </span>
              </div>
              <em>{segment.is_current ? "Now" : `${days(segment.duration_seconds, 1)}d`}</em>
            </div>
          ))}
        </div>
      </ScrollArea>

      {person.wikipedia_url ? (
        <Button asChild variant="outline" className="wiki-link">
          <a href={person.wikipedia_url} target="_blank" rel="noreferrer">
            Wikipedia <ArrowUpRight data-icon="inline-end" />
          </a>
        </Button>
      ) : null}
    </aside>
  )
}

export default function App() {
  const [data, setData] = useState<SpaceData | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [nowUnix, setNowUnix] = useState(() => Math.floor(Date.now() / 1000))

  useEffect(() => {
    let cancelled = false

    fetch("/data/space-data.json")
      .then((response) => response.json() as Promise<SpaceData>)
      .then((payload) => {
        if (!cancelled) {
          setData(payload)
          setSelectedId(
            payload.people.find((person) => person.name === "Chris Hadfield")?.id ??
              payload.people[0]?.id ??
              null,
          )
        }
      })

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const interval = window.setInterval(() => {
      setNowUnix(Math.floor(Date.now() / 1000))
    }, 60_000)

    return () => window.clearInterval(interval)
  }, [])

  const segmentsByPerson = useMemo(() => {
    const grouped = new Map<string, Segment[]>()
    for (const segment of data?.segments ?? []) {
      const current = grouped.get(segment.astronaut_id) ?? []
      current.push(segment)
      grouped.set(segment.astronaut_id, current)
    }
    return grouped
  }, [data])

  const filteredPeople = useMemo(() => {
    if (!data) return []
    const value = query.trim().toLowerCase()
    if (!value) return data.people
    return data.people.filter((person) => {
      const segments = segmentsByPerson.get(person.id) ?? []
      const missions = segments.flatMap((segment) => segment.mission_names).join(" ")
      return [person.name, person.nationality, person.first_flight_name, missions]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(value)
    })
  }, [data, query, segmentsByPerson])

  const selected =
    data?.people.find((person) => person.id === selectedId) ??
    data?.people.find((person) => person.name === "Chris Hadfield") ??
    data?.people[0]

  if (!data || !selected) {
    return (
      <main className="app-shell loading-shell">
        <div className="brand">
          <Orbit />
          <span>Humans<br />{" "}in Space</span>
        </div>
      </main>
    )
  }

  const selectedSegments = segmentsByPerson.get(selected.id) ?? []
  const currentPeople = data.people.filter((person) => person.is_currently_in_space)

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Humans in Space home">
          <Orbit />
          <span>Humans<br />{" "}in Space</span>
        </a>
        <label className="searchbox">
          <Search />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search astronaut or mission"
            aria-label="Search astronaut or mission"
          />
        </label>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <h1>Every astronaut. Every mission. One timeline.</h1>
          <div className="hero-stats" aria-label="Dataset summary">
            <HeroStat value={data.stats.astronauts.toLocaleString()} label="Astronauts" />
            <HeroStat value={data.stats.segments.toLocaleString()} label="Flights" />
            <HeroStat value={currentPeople.length.toLocaleString()} label="Humans in space" />
          </div>
        </div>
      </section>

      <CurrentSpace
        people={currentPeople}
        segmentsByPerson={segmentsByPerson}
        nowUnix={nowUnix}
        onSelect={(person) => setSelectedId(person.id)}
      />

      <div className="content-grid">
        <div className="main-column">
          <Timeline
            people={filteredPeople}
            selected={selected}
            onSelect={(person) => setSelectedId(person.id)}
          />
        </div>
        <DetailPanel person={selected} segments={selectedSegments} />
      </div>
    </main>
  )
}
