"use client";

import { useMemo, useRef, useState } from "react";
import type { Candle, EquityPoint } from "@/lib/types";

const WIDTH = 900;
const HEIGHT = 330;
const PAD = { top: 22, right: 70, bottom: 42, left: 18 };

function compactDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit" }).format(date);
}

function price(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

export function PriceChart({ candles }: { candles: Candle[] }) {
  const frame = useMemo(() => candles.slice(-90), [candles]);
  const svgRef = useRef<SVGSVGElement>(null);
  const [hovered, setHovered] = useState<number | null>(null);

  const geometry = useMemo(() => {
    if (!frame.length) return null;
    const min = Math.min(...frame.map((item) => item.low));
    const max = Math.max(...frame.map((item) => item.high));
    const range = Math.max(max - min, Math.abs(max) * 0.01, 1);
    const chartWidth = WIDTH - PAD.left - PAD.right;
    const chartHeight = HEIGHT - PAD.top - PAD.bottom;
    const x = (index: number) => PAD.left + ((index + 0.5) / frame.length) * chartWidth;
    const y = (value: number) => PAD.top + ((max - value) / range) * chartHeight;
    const pathFor = (key: "ma20" | "ma50") => {
      let path = "";
      let active = false;
      frame.forEach((item, index) => {
        const value = item[key];
        if (value === null || value === undefined) {
          active = false;
          return;
        }
        path += `${active ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)} `;
        active = true;
      });
      return path;
    };
    return { min, max, range, chartWidth, chartHeight, x, y, ma20: pathFor("ma20"), ma50: pathFor("ma50") };
  }, [frame]);

  if (!geometry || !frame.length) {
    return <div className="chart-empty">API chưa trả về lịch sử giá để vẽ biểu đồ.</div>;
  }

  const candleWidth = Math.max(2.2, Math.min(7, (geometry.chartWidth / frame.length) * 0.56));
  const chartWidth = geometry.chartWidth;
  const selected = hovered === null ? frame.at(-1) : frame[hovered];
  const selectedIndex = hovered === null ? frame.length - 1 : hovered;

  function locate(event: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const logicalX = ((event.clientX - rect.left) / rect.width) * WIDTH;
    const index = Math.floor(((logicalX - PAD.left) / chartWidth) * frame.length);
    setHovered(Math.max(0, Math.min(frame.length - 1, index)));
  }

  return (
    <div className="price-chart-wrap">
      <div className="chart-legend" aria-live="polite">
        <span>{selected ? compactDate(selected.date) : "—"}</span>
        <b>O {selected ? price(selected.open) : "—"}</b>
        <b>H {selected ? price(selected.high) : "—"}</b>
        <b>L {selected ? price(selected.low) : "—"}</b>
        <b>C {selected ? price(selected.close) : "—"}</b>
        <i className="legend-ma20">MA20</i>
        <i className="legend-ma50">MA50</i>
      </div>
      <svg
        ref={svgRef}
        className="price-chart"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Biểu đồ nến giá và đường trung bình động"
        onPointerMove={locate}
        onPointerLeave={() => setHovered(null)}
      >
        <defs>
          <linearGradient id="chartFade" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#34d6a4" stopOpacity="0.08" />
            <stop offset="1" stopColor="#34d6a4" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3, 4].map((step) => {
          const y = PAD.top + (step / 4) * geometry.chartHeight;
          const value = geometry.max - (step / 4) * geometry.range;
          return (
            <g key={step}>
              <line x1={PAD.left} y1={y} x2={WIDTH - PAD.right} y2={y} className="chart-grid" />
              <text x={WIDTH - PAD.right + 10} y={y + 4} className="chart-axis">{price(value)}</text>
            </g>
          );
        })}
        {[0, Math.floor(frame.length / 3), Math.floor((frame.length * 2) / 3), frame.length - 1].map((index) => (
          <text key={index} x={geometry.x(index)} y={HEIGHT - 12} textAnchor="middle" className="chart-axis">
            {compactDate(frame[index].date)}
          </text>
        ))}
        {frame.map((item, index) => {
          const rising = item.close >= item.open;
          const colorClass = rising ? "candle-up" : "candle-down";
          const top = geometry.y(Math.max(item.open, item.close));
          const height = Math.max(1.5, Math.abs(geometry.y(item.open) - geometry.y(item.close)));
          return (
            <g key={`${item.date}-${index}`} className={colorClass}>
              <line x1={geometry.x(index)} y1={geometry.y(item.high)} x2={geometry.x(index)} y2={geometry.y(item.low)} />
              <rect x={geometry.x(index) - candleWidth / 2} y={top} width={candleWidth} height={height} rx="0.7" />
            </g>
          );
        })}
        {geometry.ma20 && <path d={geometry.ma20} className="chart-line ma20-line" />}
        {geometry.ma50 && <path d={geometry.ma50} className="chart-line ma50-line" />}
        {selected && (
          <g className="chart-crosshair">
            <line x1={geometry.x(selectedIndex)} y1={PAD.top} x2={geometry.x(selectedIndex)} y2={HEIGHT - PAD.bottom} />
            <circle cx={geometry.x(selectedIndex)} cy={geometry.y(selected.close)} r="4" />
          </g>
        )}
      </svg>
    </div>
  );
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const frame = points.filter((item) => Number.isFinite(item.value));
  if (frame.length < 2) return <div className="chart-empty compact">API chưa trả về equity curve.</div>;
  const width = 620;
  const height = 190;
  const min = Math.min(...frame.map((item) => item.value));
  const max = Math.max(...frame.map((item) => item.value));
  const range = Math.max(max - min, 1);
  const x = (index: number) => 12 + (index / (frame.length - 1)) * (width - 24);
  const y = (value: number) => 12 + ((max - value) / range) * (height - 38);
  const line = frame.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(item.value).toFixed(1)}`).join(" ");
  const area = `${line} L${x(frame.length - 1)},${height - 18} L${x(0)},${height - 18} Z`;
  return (
    <svg className="equity-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Đường cong vốn của backtest">
      <defs>
        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#6be5bb" stopOpacity="0.25" />
          <stop offset="1" stopColor="#6be5bb" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#equityFill)" />
      <path d={line} className="equity-line" />
      <text x="12" y={height - 3} className="chart-axis">{compactDate(frame[0].date)}</text>
      <text x={width - 12} y={height - 3} textAnchor="end" className="chart-axis">{compactDate(frame.at(-1)?.date ?? "")}</text>
    </svg>
  );
}
