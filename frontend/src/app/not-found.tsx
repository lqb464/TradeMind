import Link from "next/link";
import { Icon } from "@/components/icon";

export default function NotFound() {
  return <main className="not-found"><span className="brand-mark"><Icon name="trend" /></span><p>404</p><h1>Không tìm thấy trang</h1><Link href="/">Trở về TradeMind</Link></main>;
}
