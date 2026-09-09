import type { SVGProps } from "react";

export type IconName =
  | "activity"
  | "arrow-down"
  | "arrow-up"
  | "book"
  | "brain"
  | "briefcase"
  | "check"
  | "chevron"
  | "clock"
  | "document"
  | "gauge"
  | "health"
  | "layers"
  | "lock"
  | "logout"
  | "menu"
  | "orders"
  | "refresh"
  | "search"
  | "send"
  | "shield"
  | "spark"
  | "trend"
  | "upload"
  | "user"
  | "warning"
  | "x";

const paths: Record<IconName, React.ReactNode> = {
  activity: <><path d="M3 12h4l2.2-6 4.2 12 2.4-6H21" /></>,
  "arrow-down": <><path d="m7 9 5 5 5-5" /></>,
  "arrow-up": <><path d="m7 15 5-5 5 5" /></>,
  book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z" /><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z" /></>,
  brain: <><path d="M9.5 4.5A3 3 0 0 0 4.8 7a3.5 3.5 0 0 0 .2 6.4A3 3 0 0 0 9.5 18" /><path d="M14.5 4.5A3 3 0 0 1 19.2 7a3.5 3.5 0 0 1-.2 6.4 3 3 0 0 1-4.5 4.6M12 3v18M8 9h4m4 6h-4" /></>,
  briefcase: <><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m-13 5h18M10 12v2h4v-2" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  chevron: <path d="m9 18 6-6-6-6" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  document: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v5h5M9 12h6m-6 4h6" /></>,
  gauge: <><path d="M4 17a8 8 0 1 1 16 0" /><path d="m12 15 4-5M7 17h10" /></>,
  health: <><path d="M20.8 8.2c0 5.3-8.8 11-8.8 11s-8.8-5.7-8.8-11A4.7 4.7 0 0 1 12 5.8a4.7 4.7 0 0 1 8.8 2.4Z" /><path d="M8 11h2l1-2 2 5 1-3h2" /></>,
  layers: <><path d="m12 3 9 5-9 5-9-5z" /><path d="m3 12 9 5 9-5m-18 4 9 5 9-5" /></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3" /></>,
  logout: <><path d="M10 4H5v16h5m5-4 4-4-4-4m4 4H9" /></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  orders: <><path d="M5 3h14v18H5z" /><path d="M9 8h6m-6 4h6m-6 4h4" /></>,
  refresh: <><path d="M20 7v5h-5" /><path d="M19 12a7 7 0 1 0-2 5" /></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
  send: <><path d="m3 11 18-8-8 18-2-8z" /><path d="m11 13 5-5" /></>,
  shield: <><path d="M12 3 20 6v5c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6z" /><path d="m8.5 12 2.2 2.2 4.8-5" /></>,
  spark: <><path d="m12 3 1.4 4.6L18 9l-4.6 1.4L12 15l-1.4-4.6L6 9l4.6-1.4z" /><path d="m18.5 15 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7z" /></>,
  trend: <><path d="M4 17 9 12l4 3 7-8" /><path d="M15 7h5v5" /></>,
  upload: <><path d="M12 16V4m-4 4 4-4 4 4" /><path d="M4 15v5h16v-5" /></>,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
  warning: <><path d="M12 3 2.8 20h18.4z" /><path d="M12 9v5m0 3h.01" /></>,
  x: <path d="m6 6 12 12M18 6 6 18" />,
};

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
