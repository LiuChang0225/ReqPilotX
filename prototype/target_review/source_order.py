"""Source positions for structural nodes that have no authored ordering value.

Array indexes and chapter numbers are never positions in the document. Existing
explicit positions remain authoritative; missing ones use the same page/point
scale as original-item positions in the candidate contract.
"""
from __future__ import annotations


def position(page, top=0):
    return page * 1_000_000 + top * 100


def section_positions(bundle):
    sections = {section['id']: section for section in bundle['outline']}
    lines = bundle.get('lines', {})
    by_section = {key: [] for key in sections}
    children = {key: [] for key in sections}
    for item in bundle['items']:
        refs = item.get('sourceRefs', [])
        points = [position(ref['page'], rect[1]) for ref in refs for rect in ref.get('rects', [])]
        value = item.get('reviewMetadata', {}).get('sourceOrder')
        if value is not None:
            by_section[item['sectionId']].append(value)
        elif points:
            by_section[item['sectionId']].append(min(points))
    for section in sections.values():
        if section.get('parentId') in children:
            children[section['parentId']].append(section['id'])
    for table in bundle.get('reviewTables', []):
        if table['sectionId'] in by_section:
            by_section[table['sectionId']].extend(
                position(segment['page'], segment.get('bbox', [0, 0])[1])
                for segment in table['segments'])
    orders, active = {}, set()
    def resolve(key):
        if key in orders:
            return orders[key]
        if key in active:
            raise ValueError('Cyclic source structure')
        active.add(key)
        section = sections[key]
        value = section.get('sourceOrder')
        if value is None:
            anchors = [lines[lid] for lid in section.get('headingLineIds', []) if lid in lines]
            points = [position(line['page'], line['bbox'][1]) for line in anchors if line.get('bbox')]
            if points:
                value = min(points)
            else:
                points = by_section[key] + [resolve(child) for child in children[key]]
                # A declared earlier start page still precedes later body content.
                value = min(points) if points else position(section['page'])
                if value // 1_000_000 > section['page']:
                    value = position(section['page'])
        active.remove(key)
        orders[key] = value
        return value
    for key in sections:
        resolve(key)
    return orders


def freeze_section_positions(bundle):
    orders = section_positions(bundle)
    changed = []
    for section in bundle['outline']:
        if 'sourceOrder' not in section:
            section['sourceOrder'] = orders[section['id']]
            changed.append(section['id'])
    return changed
