'use client';

import { useState, useMemo } from 'react';
import Link from 'next/link';

export interface CalendarEvent {
  id: string;
  name: string;
  description?: string | null;
  venue?: string | null;
  startsAt: string;
  endsAt: string;
  status: string;
  categories?: Array<{
    name: string;
    basePricePaise: number;
    availability: number;
  }>;
}

interface EventCalendarProps {
  initialEvents: CalendarEvent[];
}

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'
];

const DAYS_OF_WEEK = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function EventCalendar({ initialEvents }: EventCalendarProps) {
  const [currentDate, setCurrentDate] = useState(() => new Date());
  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);

  const year = currentDate.getFullYear();
  const month = currentDate.getMonth();

  const handlePrevMonth = () => {
    setCurrentDate(new Date(year, month - 1, 1));
  };

  const handleNextMonth = () => {
    setCurrentDate(new Date(year, month + 1, 1));
  };

  const handleToday = () => {
    setCurrentDate(new Date());
  };

  // Group events by YYYY-MM-DD
  const eventsByDate = useMemo(() => {
    const map = new Map<string, CalendarEvent[]>();
    for (const ev of initialEvents) {
      if (!ev.startsAt) continue;
      const d = new Date(ev.startsAt);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const list = map.get(key) || [];
      list.push(ev);
      map.set(key, list);
    }
    return map;
  }, [initialEvents]);

  // Calendar cells
  const calendarCells = useMemo(() => {
    const firstDayIndex = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const prevMonthDays = new Date(year, month, 0).getDate();

    const cells: Array<{
      dateStr: string;
      dayNum: number;
      isCurrentMonth: boolean;
      isToday: boolean;
      events: CalendarEvent[];
    }> = [];

    const today = new Date();
    const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

    // Leading days from previous month
    for (let i = firstDayIndex - 1; i >= 0; i--) {
      const d = prevMonthDays - i;
      const prevMonth = month === 0 ? 11 : month - 1;
      const prevYear = month === 0 ? year - 1 : year;
      const key = `${prevYear}-${String(prevMonth + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      cells.push({
        dateStr: key,
        dayNum: d,
        isCurrentMonth: false,
        isToday: key === todayKey,
        events: eventsByDate.get(key) || []
      });
    }

    // Days in current month
    for (let d = 1; d <= daysInMonth; d++) {
      const key = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      cells.push({
        dateStr: key,
        dayNum: d,
        isCurrentMonth: true,
        isToday: key === todayKey,
        events: eventsByDate.get(key) || []
      });
    }

    // Trailing days from next month to complete 35 or 42 cells
    const remaining = (7 - (cells.length % 7)) % 7;
    for (let d = 1; d <= remaining; d++) {
      const nextMonth = month === 11 ? 0 : month + 1;
      const nextYear = month === 11 ? year + 1 : year;
      const key = `${nextYear}-${String(nextMonth + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      cells.push({
        dateStr: key,
        dayNum: d,
        isCurrentMonth: false,
        isToday: key === todayKey,
        events: eventsByDate.get(key) || []
      });
    }

    return cells;
  }, [year, month, eventsByDate]);

  const formatTimeIST = (iso: string) => {
    try {
      const date = new Date(iso);
      return date.toLocaleTimeString('en-IN', {
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'Asia/Kolkata',
        hour12: true
      });
    } catch {
      return iso;
    }
  };

  const formatDateIST = (iso: string) => {
    try {
      const date = new Date(iso);
      return date.toLocaleDateString('en-IN', {
        weekday: 'short',
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        timeZone: 'Asia/Kolkata'
      });
    } catch {
      return iso;
    }
  };

  return (
    <div className="mt-8 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
      {/* Header with Navigation */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between pb-6 border-b border-slate-100">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-900">
            {MONTH_NAMES[month]} {year}
          </h2>
          <p className="text-sm text-slate-500">Events and activity schedules across the campus</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleToday}
            className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-100 transition"
          >
            Today
          </button>
          <div className="flex items-center rounded-lg border border-slate-200 bg-white">
            <button
              type="button"
              onClick={handlePrevMonth}
              aria-label="Previous month"
              className="p-1.5 text-slate-600 hover:bg-slate-50 rounded-l-lg transition"
            >
              ←
            </button>
            <div className="h-4 w-px bg-slate-200" />
            <button
              type="button"
              onClick={handleNextMonth}
              aria-label="Next month"
              className="p-1.5 text-slate-600 hover:bg-slate-50 rounded-r-lg transition"
            >
              →
            </button>
          </div>
        </div>
      </div>

      {/* Weekday Labels */}
      <div className="grid grid-cols-7 gap-px pt-4 text-center text-xs font-semibold uppercase text-slate-500">
        {DAYS_OF_WEEK.map((day) => (
          <div key={day} className="py-2">
            {day}
          </div>
        ))}
      </div>

      {/* Calendar Grid */}
      <div className="grid grid-cols-7 gap-1 pt-1">
        {calendarCells.map((cell, idx) => (
          <div
            key={idx}
            className={`min-h-[90px] rounded-xl border p-2 transition flex flex-col justify-between ${
              cell.isCurrentMonth ? 'bg-white border-slate-100' : 'bg-slate-50/60 border-transparent text-slate-400'
            } ${cell.isToday ? 'ring-2 ring-teal-500 bg-teal-50/20' : ''}`}
          >
            <div className="flex items-center justify-between">
              <span
                className={`text-xs font-bold ${
                  cell.isToday
                    ? 'inline-flex h-6 w-6 items-center justify-center rounded-full bg-teal-600 text-white'
                    : cell.isCurrentMonth
                    ? 'text-slate-800'
                    : 'text-slate-400'
                }`}
              >
                {cell.dayNum}
              </span>
              {cell.events.length > 0 && (
                <span className="inline-flex h-2 w-2 rounded-full bg-teal-500" />
              )}
            </div>

            <div className="mt-1 space-y-1">
              {cell.events.slice(0, 2).map((ev) => (
                <button
                  key={ev.id}
                  type="button"
                  onClick={() => setSelectedEvent(ev)}
                  className="w-full text-left truncate rounded bg-teal-50 px-1.5 py-0.5 text-[11px] font-medium text-teal-800 hover:bg-teal-100 transition block"
                >
                  {ev.name}
                </button>
              ))}
              {cell.events.length > 2 && (
                <span className="text-[10px] text-slate-500 block text-right">
                  +{cell.events.length - 2} more
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Selected Event Details Modal / Card */}
      {selectedEvent && (
        <div className="mt-6 rounded-xl border border-teal-200 bg-teal-50/40 p-5">
          <div className="flex items-start justify-between">
            <div>
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-teal-100 px-2 py-0.5 text-[11px] font-semibold text-teal-800 uppercase">
                  {selectedEvent.status}
                </span>
                <span className="text-xs text-slate-500">
                  {formatDateIST(selectedEvent.startsAt)}
                </span>
              </div>
              <h3 className="mt-1 text-xl font-bold text-slate-900">{selectedEvent.name}</h3>
            </div>
            <button
              type="button"
              onClick={() => setSelectedEvent(null)}
              className="text-slate-400 hover:text-slate-600 text-sm font-semibold p-1"
            >
              ✕
            </button>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 text-sm">
            <div>
              <span className="text-xs font-medium uppercase tracking-wider text-slate-500 block">Time (IST)</span>
              <p className="font-semibold text-slate-800">
                {formatTimeIST(selectedEvent.startsAt)} – {formatTimeIST(selectedEvent.endsAt)}
              </p>
            </div>
            <div>
              <span className="text-xs font-medium uppercase tracking-wider text-slate-500 block">Campus Venue</span>
              <p className="font-semibold text-slate-800">
                📍 {selectedEvent.venue || 'Campus Hall (TBA)'}
              </p>
            </div>
          </div>

          {selectedEvent.description && (
            <p className="mt-3 text-sm text-slate-600 leading-relaxed">
              {selectedEvent.description}
            </p>
          )}

          <div className="mt-5 flex items-center justify-between pt-4 border-t border-teal-100">
            <span className="text-xs text-slate-500">
              FairSeat capacity protected
            </span>
            <Link
              href="/bookings"
              className="rounded-lg bg-teal-600 px-4 py-2 text-xs font-bold text-white shadow-sm hover:bg-teal-700 transition"
            >
              Book Tickets →
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
