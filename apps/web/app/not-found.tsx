// An address the dashboard doesn't have: say so plainly and offer every
// screen it does have, so a stale bookmark is one click from the right place.
import Link from "next/link";
import { Icon } from "@/components/Icon";
import { navGroups } from "@/lib/nav";

export const metadata = { title: "Not found" };

export default function NotFound() {
  return (
    <section className="panel panel--spine panel--warn">
      <div className="panel__body form form--wide">
        <p className="eyebrow">404 · not found</p>
        <h1 className="page-head__title">There’s no screen at this address</h1>
        <p className="text-dim">The link may be old or mistyped. Every screen the dashboard has is listed below.</p>
        <nav className="nf__groups" aria-label="All screens">
          {navGroups.map((g) => (
            <div key={g.label} className="nf__group">
              <p className="nf__label">{g.label}</p>
              {g.items.map((item) => (
                <Link key={item.key} href={item.href} className="nf__link">
                  <Icon name={item.icon} size={15} />
                  <span>
                    {item.label}
                    <span className="nf__hint">{item.hint}</span>
                  </span>
                </Link>
              ))}
            </div>
          ))}
        </nav>
        <div className="btn-row">
          <Link className="btn btn--primary" href="/">
            Go to the overview
          </Link>
        </div>
      </div>
    </section>
  );
}
