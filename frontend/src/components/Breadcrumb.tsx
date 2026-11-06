import { Fragment } from "react";
import type { BreadcrumbItem } from "../types/graph";

const KIND_MARKS: Record<BreadcrumbItem["kind"], string> = {
  project: "⌂",
  module: "▣",
  class: "◆",
  function: "ƒ",
  method: "↳",
  component: "◆",
  stage: "▸",
  condition: "◇",
};

interface BreadcrumbProps {
  items: BreadcrumbItem[];
  onNavigate: (nodeId: string | null) => void;
}

export function Breadcrumb({ items, onNavigate }: BreadcrumbProps) {
  return (
    <nav className="breadcrumb" aria-label="导航面包屑">
      {items.map((item, index) => (
        <Fragment key={`${item.kind}:${item.id ?? "root"}`}>
          {index > 0 ? (
            <span className="breadcrumb__separator" aria-hidden="true">
              ›
            </span>
          ) : null}
          <button
            type="button"
            className={`breadcrumb__item${item.isCurrent ? " is-current" : ""}`}
            disabled={!item.clickable}
            aria-current={item.isCurrent ? "page" : undefined}
            onClick={() => {
              if (item.clickable) {
                onNavigate(item.id);
              }
            }}
          >
            <span className="breadcrumb__mark" aria-hidden="true">
              {KIND_MARKS[item.kind]}
            </span>
            {item.label}
          </button>
        </Fragment>
      ))}
    </nav>
  );
}
