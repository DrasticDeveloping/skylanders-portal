#include "nav/nav_planner.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <initializer_list>
#include <limits>
#include <map>
#include <unordered_set>
#include <utility>

namespace skylanders::nav {

namespace {

// How far outside an edge (in world units) a point still counts as on it: points on a shared edge
// or a polygon corner belong to the areas on both sides.
constexpr float kEdgeSlack = 0.01f;
// Two points closer than this in xy are the same corner for the funnel.
constexpr float kSamePointSq = 1e-6f;
constexpr float kInf = std::numeric_limits<float>::infinity();

float Dist(const Vec3& a, const Vec3& b) {
  const float dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
  return std::sqrt(dx * dx + dy * dy + dz * dz);
}

float DistSqXY(const Vec3& a, const Vec3& b) {
  const float dx = b.x - a.x, dy = b.y - a.y;
  return dx * dx + dy * dy;
}

Vec3 Midpoint(const Vec3& a, const Vec3& b) {
  return {(a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f, (a.z + b.z) * 0.5f};
}

Vec3 Lerp(const Vec3& a, const Vec3& b, float t) {
  return {a.x + t * (b.x - a.x), a.y + t * (b.y - a.y), a.z + t * (b.z - a.z)};
}

bool Finite(const Vec3& v) {
  return std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z);
}

// Twice the signed area of the triangle a, b, c in xy: positive when c is left of a->b
// (counter-clockwise), negative when right, 0 when the three are on a line.
float Cross(const Vec3& a, const Vec3& b, const Vec3& c) {
  return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

bool SamePointXY(const Vec3& a, const Vec3& b) { return DistSqXY(a, b) < kSamePointSq; }

bool SamePoint(const Vec3& a, const Vec3& b) {
  const float dz = b.z - a.z;
  return SamePointXY(a, b) && dz * dz < kSamePointSq;
}

// True when c lies on the segment p-q in xy (within kEdgeSlack of its line, between its ends).
bool OnSegmentXY(const Vec3& p, const Vec3& c, const Vec3& q) {
  const float len = std::sqrt(DistSqXY(p, q));
  if (len < kEdgeSlack) return SamePointXY(p, c);
  if (std::fabs(Cross(p, q, c)) > kEdgeSlack * len) return false;  // Cross = len x distance
  const float t = (c.x - p.x) * (q.x - p.x) + (c.y - p.y) * (q.y - p.y);
  return t >= 0.0f && t <= len * len;
}

// The distance in xy from p to the segment a-b.
float SegmentDistXY(const Vec3& p, const Vec3& a, const Vec3& b) {
  const float dx = b.x - a.x, dy = b.y - a.y;
  const float len_sq = dx * dx + dy * dy;
  float t = len_sq > 0.0f ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len_sq : 0.0f;
  t = std::clamp(t, 0.0f, 1.0f);
  const float qx = a.x + t * dx - p.x, qy = a.y + t * dy - p.y;
  return std::sqrt(qx * qx + qy * qy);
}

// The closest points in xy of the segments p0-p1 and q0-q1 (both of non-zero length), as the
// parameters s along p and t along q (after Ericson, Real-Time Collision Detection 5.1.9). For
// parallel segments every point of the overlap is as close as any other; s is the middle of the
// overlap then (or the nearer end when they do not overlap), so a jump across a straight gap takes
// off from the middle of it, not from a corner.
void ClosestParamsXY(const Vec3& p0, const Vec3& p1, const Vec3& q0, const Vec3& q1, float& s,
                     float& t) {
  const float dx = p1.x - p0.x, dy = p1.y - p0.y, ex = q1.x - q0.x, ey = q1.y - q0.y;
  const float rx = p0.x - q0.x, ry = p0.y - q0.y;
  const float a = dx * dx + dy * dy, e = ex * ex + ey * ey;
  const float b = dx * ex + dy * ey, c = dx * rx + dy * ry, f = ex * rx + ey * ry;
  const float denom = a * e - b * b;
  if (denom <= 1e-6f * a * e) {
    float s0 = ((q0.x - p0.x) * dx + (q0.y - p0.y) * dy) / a;
    float s1 = ((q1.x - p0.x) * dx + (q1.y - p0.y) * dy) / a;
    if (s0 > s1) std::swap(s0, s1);
    const float lo = std::max(0.0f, s0), hi = std::min(1.0f, s1);
    s = lo <= hi ? (lo + hi) * 0.5f : (s1 < 0.0f ? 0.0f : 1.0f);
    t = std::clamp(((p0.x + s * dx - q0.x) * ex + (p0.y + s * dy - q0.y) * ey) / e, 0.0f, 1.0f);
    return;
  }
  s = std::clamp((b * f - c * e) / denom, 0.0f, 1.0f);
  t = (b * s + f) / e;
  if (t < 0.0f) {
    t = 0.0f;
    s = std::clamp(-c / a, 0.0f, 1.0f);
  } else if (t > 1.0f) {
    t = 1.0f;
    s = std::clamp((b - c) / a, 0.0f, 1.0f);
  }
}

// The portal from one area into the next, as seen walking through it: left and right of the
// walker.
struct Portal {
  Vec3 left, right;
};

// One step of an area path: leave `area` across its edge `edge`, or (edge -1) by the link with
// index `link` into the mesh's links.
struct Step {
  int area;
  int edge;
  int link;
};

bool InRange(int index, size_t count) { return index >= 0 && size_t(index) < count; }

// What A* searches: the areas with their neighbor links, and the enabled links out of each area.
struct Graph {
  const std::vector<Area>& areas;
  const std::vector<Link>& links;
  const std::vector<std::vector<int>>& out_links;  // per area, indices into links
};

// A* over the areas from `from` (the walker at `start`) to `to` (the target point `goal`).
// The search state is an area together with the point where the path entered it: the start for
// the first area, the midpoint of the shared edge or a link's landing for the others. A step across
// an edge costs the distance from that entry point to the midpoint of the edge; a link costs the
// distance from the entry point to its takeoff plus the link's own cost. The heuristic is the
// straight distance from the entry point to the goal times h_scale (below 1 when a link is cheaper
// than its length, so the heuristic never overestimates), and reaching the goal area adds the
// distance from its entry point to the goal. The open list is a binary heap with stale entries
// skipped when popped. Returns false when `to` cannot be reached over the (one-way) neighbor links
// and links.
bool SearchAreas(const Graph& graph, int from, const Vec3& start, int to, const Vec3& goal,
                 float h_scale, std::vector<Step>& steps) {
  const std::vector<Area>& areas = graph.areas;
  const size_t n = areas.size();
  std::vector<float> cost(n, kInf);  // best path length to the area's entry point so far
  std::vector<Vec3> entry(n);        // that entry point
  std::vector<Step> came_from(n, {-1, -1, -1});

  struct Open {
    float f;  // cost + heuristic
    float g;  // cost when pushed (stale once the area's cost dropped below it)
    int area;
  };
  const auto later = [](const Open& a, const Open& b) { return a.f > b.f; };  // min-heap on f
  std::vector<Open> heap;
  const auto relax = [&](int v, float g, const Vec3& at, const Step& via) {
    if (!(g < cost[v])) return;
    cost[v] = g;
    entry[v] = at;
    came_from[v] = via;
    heap.push_back({g + h_scale * Dist(at, goal), g, v});
    std::push_heap(heap.begin(), heap.end(), later);
  };

  cost[from] = 0.0f;
  entry[from] = start;
  heap.push_back({h_scale * Dist(start, goal), 0.0f, from});

  while (!heap.empty()) {
    std::pop_heap(heap.begin(), heap.end(), later);
    const Open top = heap.back();
    heap.pop_back();
    if (top.g > cost[top.area]) continue;  // a better way to this area was found after the push

    if (top.area == to) {
      // Walk the parents back. Each parent's cost is at most its child's, so this ends; the bound
      // guards against a degenerate mesh with zero-length steps or zero-cost links.
      steps.clear();
      for (int a = to; a != from && steps.size() <= n; a = came_from[a].area) {
        steps.push_back(came_from[a]);
      }
      std::reverse(steps.begin(), steps.end());
      return true;
    }

    const int u = top.area;
    const std::vector<Edge>& edges = areas[u].edges;
    for (size_t e = 0; e < edges.size(); ++e) {
      const int v = edges[e].neighbor;
      if (!InRange(v, n) || v == u) continue;
      const Vec3 mid = Midpoint(edges[e].a, edges[e].b);
      relax(v, cost[u] + Dist(entry[u], mid), mid, {u, int(e), -1});
    }
    if (size_t(u) < graph.out_links.size()) {
      for (int li : graph.out_links[u]) {
        const Link& link = graph.links[li];
        relax(link.to_area, cost[u] + Dist(entry[u], link.takeoff) + link.cost, link.landing,
              {u, -1, li});
      }
    }
  }
  return false;
}

// The simple stupid funnel (string pulling, after Mikko Mononen) in xy. portals[0] and the last
// portal are the start and the goal (left == right). The funnel keeps an apex and the left and
// right sides seen from it; each portal narrows a side, and when a side would cross the other, the
// other side's point becomes a corner (a waypoint), the new apex, and the scan restarts from the
// portal after it. Returns the corners and then the goal, start left out, with no two in a row the
// same.
std::vector<Vec3> PullString(const std::vector<Portal>& portals, const Vec3& start) {
  std::vector<Vec3> out;
  const auto add = [&](const Vec3& p) {
    const Vec3& last = out.empty() ? start : out.back();
    if (!SamePoint(last, p)) out.push_back(p);
  };

  Vec3 apex = portals[0].left, left = portals[0].left, right = portals[0].right;
  size_t apex_i = 0, left_i = 0, right_i = 0;
  // Every restart moves the apex to a later portal, so the scan is at most quadratic; the bound
  // only guards against floating-point surprises.
  size_t budget = portals.size() * portals.size() + 16;

  for (size_t i = 1; i < portals.size() && budget > 0; ++i, --budget) {
    const Vec3& l = portals[i].left;
    const Vec3& r = portals[i].right;

    // The right side: narrow it if the new right point is left of (or on) it.
    if (Cross(apex, right, r) >= 0.0f) {
      if (SamePointXY(apex, right) || Cross(apex, left, r) < 0.0f) {
        right = r;
        right_i = i;
      } else {
        // r crosses the left side: the left point is a corner.
        apex = left;
        apex_i = left_i;
        add(apex);
        right = left = apex;
        right_i = left_i = apex_i;
        i = apex_i;
        continue;
      }
    }

    // The left side, the same mirrored.
    if (Cross(apex, left, l) <= 0.0f) {
      if (SamePointXY(apex, left) || Cross(apex, right, l) > 0.0f) {
        left = l;
        left_i = i;
      } else {
        apex = right;
        apex_i = right_i;
        add(apex);
        left = right = apex;
        left_i = right_i = apex_i;
        i = apex_i;
        continue;
      }
    }
  }

  // The goal is always the last waypoint, even when it is the start.
  const Vec3& goal = portals.back().left;
  if (out.empty() || !SamePoint(out.back(), goal)) out.push_back(goal);

  // Drop corners that lie on the straight line between their neighbours. The funnel makes one
  // where a portal's end lies exactly on a side (on a diagonal across a grid of squares the path
  // passes through vertices); walking through it changes nothing.
  std::vector<Vec3> kept;
  kept.reserve(out.size());
  Vec3 prev = start;
  for (size_t k = 0; k < out.size(); ++k) {
    if (k + 1 < out.size() && OnSegmentXY(prev, out[k], out[k + 1])) continue;
    kept.push_back(out[k]);
    prev = out[k];
  }
  return kept;
}

std::string Format(const char* fmt, double a, double b = 0.0, double c = 0.0) {
  char buf[160];
  std::snprintf(buf, sizeof(buf), fmt, a, b, c);
  return buf;
}

Action ActionFor(LinkKind kind) {
  switch (kind) {
    case LinkKind::kJump:
      return Action::kJump;
    case LinkKind::kDrop:
      return Action::kDrop;
    case LinkKind::kLink:
      break;
  }
  return Action::kLink;
}

// A uniform grid of buckets over xy for the gap search: each item goes into every cell its box
// touches, and a query hands out every item of every cell its box touches (an item can come more
// than once; the caller drops repeats).
class Grid2D {
 public:
  Grid2D(float min_x, float min_y, float max_x, float max_y, float cell) {
    // Keep the grid at a million cells or fewer whatever the level's size.
    const float w = std::max(max_x - min_x, 1.0f), h = std::max(max_y - min_y, 1.0f);
    cell_ = std::max(cell, 1.0f);
    while ((w / cell_ + 1.0f) * (h / cell_ + 1.0f) > 1048576.0f) cell_ *= 2.0f;
    min_x_ = min_x;
    min_y_ = min_y;
    nx_ = int(w / cell_) + 1;
    ny_ = int(h / cell_) + 1;
    cells_.resize(size_t(nx_) * size_t(ny_));
  }

  void Insert(int item, float x0, float y0, float x1, float y1) {
    int i0, j0, i1, j1;
    Range(x0, y0, x1, y1, i0, j0, i1, j1);
    for (int j = j0; j <= j1; ++j) {
      for (int i = i0; i <= i1; ++i) cells_[size_t(j) * nx_ + i].push_back(item);
    }
  }

  template <class F>
  void Query(float x0, float y0, float x1, float y1, F&& f) const {
    int i0, j0, i1, j1;
    Range(x0, y0, x1, y1, i0, j0, i1, j1);
    for (int j = j0; j <= j1; ++j) {
      for (int i = i0; i <= i1; ++i) {
        for (int item : cells_[size_t(j) * nx_ + i]) f(item);
      }
    }
  }

 private:
  int Clamp(float v, float lo, int count) const {
    const float c = std::floor((v - lo) / cell_);
    if (!(c > 0.0f)) return 0;  // also NaN
    return c >= float(count - 1) ? count - 1 : int(c);
  }
  void Range(float x0, float y0, float x1, float y1, int& i0, int& j0, int& i1, int& j1) const {
    i0 = Clamp(x0, min_x_, nx_);
    i1 = Clamp(x1, min_x_, nx_);
    j0 = Clamp(y0, min_y_, ny_);
    j1 = Clamp(y1, min_y_, ny_);
  }

  float min_x_ = 0, min_y_ = 0, cell_ = 1;
  int nx_ = 1, ny_ = 1;
  std::vector<std::vector<int>> cells_;
};

// The walking length between two points over the neighbor links only, searched up to a bound: the
// same entry-point metric as SearchAreas, without links. The buffers are kept between calls and
// only the touched areas are reset.
class WalkSearch {
 public:
  explicit WalkSearch(const std::vector<Area>& areas)
      : areas_(areas), cost_(areas.size(), kInf), entry_(areas.size()) {}

  // The length from start (in area from) to goal (in area to) when it is at most bound;
  // infinity otherwise.
  float Length(int from, const Vec3& start, int to, const Vec3& goal, float bound) {
    for (int a : touched_) cost_[a] = kInf;
    touched_.clear();
    heap_.clear();
    const auto later = [](const Open& a, const Open& b) { return a.f > b.f; };
    const auto push = [&](int v, float g, const Vec3& at) {
      const float f = g + Dist(at, goal);
      if (f > bound || !(g < cost_[v])) return;
      if (cost_[v] == kInf) touched_.push_back(v);
      cost_[v] = g;
      entry_[v] = at;
      heap_.push_back({f, g, v});
      std::push_heap(heap_.begin(), heap_.end(), later);
    };
    push(from, 0.0f, start);
    while (!heap_.empty()) {
      std::pop_heap(heap_.begin(), heap_.end(), later);
      const Open top = heap_.back();
      heap_.pop_back();
      if (top.g > cost_[top.area]) continue;
      if (top.area == to) return top.f;  // f = the walk to the entry plus the rest to the goal
      const std::vector<Edge>& edges = areas_[top.area].edges;
      for (const Edge& e : edges) {
        if (!InRange(e.neighbor, areas_.size()) || e.neighbor == top.area) continue;
        const Vec3 mid = Midpoint(e.a, e.b);
        push(e.neighbor, top.g + Dist(entry_[top.area], mid), mid);
      }
    }
    return kInf;
  }

 private:
  struct Open {
    float f, g;
    int area;
  };
  const std::vector<Area>& areas_;
  std::vector<float> cost_;
  std::vector<Vec3> entry_;
  std::vector<int> touched_;
  std::vector<Open> heap_;
};

}  // namespace

NavMesh::NavMesh(std::vector<Area> areas) : areas_(std::move(areas)) {
  const size_t n = areas_.size();

  // Shapes: bounding box, orientation and plane of each polygon.
  shapes_.resize(n);
  for (size_t i = 0; i < n; ++i) {
    const std::vector<Vec3>& poly = areas_[i].polygon;
    Shape& s = shapes_[i];
    s = {0, 0, 0, 0, {0, 0, 1}, areas_[i].center, true};
    if (poly.empty()) continue;
    s.min_x = s.max_x = poly[0].x;
    s.min_y = s.max_y = poly[0].y;
    Vec3 sum{0, 0, 0}, normal{0, 0, 0};
    for (size_t k = 0; k < poly.size(); ++k) {
      const Vec3& p = poly[k];
      const Vec3& q = poly[(k + 1) % poly.size()];
      s.min_x = std::min(s.min_x, p.x);
      s.max_x = std::max(s.max_x, p.x);
      s.min_y = std::min(s.min_y, p.y);
      s.max_y = std::max(s.max_y, p.y);
      sum.x += p.x;
      sum.y += p.y;
      sum.z += p.z;
      // Newell's method: the normal of a (nearly) planar polygon; its z is twice the signed area
      // in xy, positive for counter-clockwise.
      normal.x += (p.y - q.y) * (p.z + q.z);
      normal.y += (p.z - q.z) * (p.x + q.x);
      normal.z += (p.x - q.x) * (p.y + q.y);
    }
    const float count = float(poly.size());
    s.origin = {sum.x / count, sum.y / count, sum.z / count};
    s.ccw = normal.z >= 0.0f;
    if (normal.z < 0.0f) normal = {-normal.x, -normal.y, -normal.z};
    s.normal = normal;
  }

  ComputeIslands();
}

void NavMesh::ComputeIslands() {
  const size_t n = areas_.size();

  // The graph: neighbor links and enabled links, each taken in both directions (a one-way link
  // still joins two areas into one island; Symmetric() reports one-way neighbor links, and A*
  // follows every link only the way it points).
  std::vector<std::vector<int>> joined(n);
  for (size_t i = 0; i < n; ++i) {
    for (const Edge& e : areas_[i].edges) {
      if (!ValidNeighbor(int(i), e.neighbor)) continue;
      joined[i].push_back(e.neighbor);
      joined[e.neighbor].push_back(int(i));
    }
  }
  out_links_.assign(n, {});
  for (size_t k = 0; k < links_.size(); ++k) {
    if (link_disabled_[k]) continue;
    const Link& link = links_[k];
    out_links_[link.from_area].push_back(int(k));
    joined[link.from_area].push_back(link.to_area);
    joined[link.to_area].push_back(link.from_area);
  }

  island_.assign(n, -1);
  island_count_ = 0;
  std::vector<int> stack;
  for (size_t seed = 0; seed < n; ++seed) {
    if (island_[seed] != -1) continue;
    const int id = int(island_count_++);
    island_[seed] = id;
    stack.push_back(int(seed));
    while (!stack.empty()) {
      const int a = stack.back();
      stack.pop_back();
      for (int b : joined[a]) {
        if (island_[b] != -1) continue;
        island_[b] = id;
        stack.push_back(b);
      }
    }
  }
}

void NavMesh::AddLinks(std::vector<Link> links) {
  std::unordered_set<int> used;
  for (const Link& l : links_) used.insert(l.id);
  for (Link& l : links) {
    if (!InRange(l.from_area, areas_.size()) || !ValidNeighbor(l.from_area, l.to_area)) continue;
    if (!Finite(l.takeoff) || !Finite(l.landing)) continue;
    if (!(l.cost >= 0.0f) || !std::isfinite(l.cost)) l.cost = Dist(l.takeoff, l.landing);
    if (l.id < 0 || used.count(l.id)) l.id = next_link_id_;
    used.insert(l.id);
    next_link_id_ = std::max(next_link_id_, l.id + 1);
    links_.push_back(l);
    link_disabled_.push_back(0);
  }
  ComputeIslands();
}

bool NavMesh::DisableLink(int id) {
  for (size_t k = 0; k < links_.size(); ++k) {
    if (links_[k].id != id) continue;
    if (!link_disabled_[k]) {
      link_disabled_[k] = 1;
      ComputeIslands();
    }
    return true;
  }
  return false;
}

bool NavMesh::LinkDisabled(int id) const {
  for (size_t k = 0; k < links_.size(); ++k) {
    if (links_[k].id == id) return link_disabled_[k] != 0;
  }
  return false;
}

int NavMesh::Island(int area) const { return InRange(area, areas_.size()) ? island_[area] : -1; }

bool NavMesh::ContainsXY(int area, float x, float y) const {
  const Shape& s = shapes_[area];
  if (x < s.min_x - kEdgeSlack || x > s.max_x + kEdgeSlack || y < s.min_y - kEdgeSlack ||
      y > s.max_y + kEdgeSlack) {
    return false;
  }
  const std::vector<Vec3>& poly = areas_[area].polygon;
  if (poly.size() < 3) return false;
  const Vec3 p{x, y, 0.0f};
  for (size_t k = 0; k < poly.size(); ++k) {
    const Vec3& a = poly[k];
    const Vec3& b = poly[(k + 1) % poly.size()];
    // Cross() is the edge length times the signed distance of p from the edge's line.
    const float len = std::sqrt(DistSqXY(a, b));
    const float side = Cross(a, b, p);
    if (s.ccw ? side < -kEdgeSlack * len : side > kEdgeSlack * len) return false;
  }
  return true;
}

float NavMesh::FloorZ(int area, float x, float y) const {
  const Shape& s = shapes_[area];
  const float length = std::sqrt(s.normal.x * s.normal.x + s.normal.y * s.normal.y +
                                 s.normal.z * s.normal.z);
  if (s.normal.z <= length * 1e-6f) return s.origin.z;  // vertical or degenerate polygon
  // The plane through origin with this normal, solved for z.
  return s.origin.z - (s.normal.x * (x - s.origin.x) + s.normal.y * (y - s.origin.y)) / s.normal.z;
}

Vec3 NavMesh::ClosestPointXY(int area, float x, float y) const {
  if (ContainsXY(area, x, y)) return {x, y, FloorZ(area, x, y)};
  const std::vector<Vec3>& poly = areas_[area].polygon;
  if (poly.empty()) return areas_[area].center;
  const Vec3 p{x, y, 0.0f};
  Vec3 best = poly[0];
  float best_d = DistSqXY(best, p);
  for (size_t k = 0; k < poly.size(); ++k) {
    const Vec3& a = poly[k];
    const Vec3& b = poly[(k + 1) % poly.size()];
    const float dx = b.x - a.x, dy = b.y - a.y;
    const float len_sq = dx * dx + dy * dy;
    float t = len_sq > 0.0f ? ((x - a.x) * dx + (y - a.y) * dy) / len_sq : 0.0f;
    t = std::clamp(t, 0.0f, 1.0f);
    // On the edge, z runs linearly between its two vertices.
    const Vec3 q{a.x + t * dx, a.y + t * dy, a.z + t * (b.z - a.z)};
    const float d = DistSqXY(q, p);
    if (d < best_d) {
      best_d = d;
      best = q;
    }
  }
  return best;
}

int NavMesh::FindArea(const Vec3& p, float max_dz) const {
  int best = -1;
  float best_dz = max_dz;
  for (size_t i = 0; i < areas_.size(); ++i) {
    if (!ContainsXY(int(i), p.x, p.y)) continue;
    const float dz = std::fabs(FloorZ(int(i), p.x, p.y) - p.z);
    if (dz <= best_dz && (best == -1 || dz < best_dz)) {
      best = int(i);
      best_dz = dz;
    }
  }
  return best;
}

float NavMesh::WalkLength(int from_area, const Vec3& from, int to_area, const Vec3& to,
                          float bound) const {
  if (!InRange(from_area, areas_.size()) || !InRange(to_area, areas_.size())) return kInf;
  if (!Finite(from) || !Finite(to) || !(bound >= 0.0f)) return kInf;
  WalkSearch walk(areas_);
  return walk.Length(from_area, from, to_area, to, bound);
}

bool NavMesh::Symmetric() const {
  for (size_t i = 0; i < areas_.size(); ++i) {
    for (const Edge& e : areas_[i].edges) {
      if (e.neighbor < 0) continue;                         // a boundary
      if (!ValidNeighbor(int(i), e.neighbor)) return false;  // out of range, or the area itself
      const std::vector<Edge>& back = areas_[e.neighbor].edges;
      const bool found = std::any_of(back.begin(), back.end(),
                                     [&](const Edge& r) { return r.neighbor == int(i); });
      if (!found) return false;
    }
  }
  return true;
}

Plan PlanPath(const NavMesh& mesh, const Vec3& start, const Vec3& goal) {
  Plan plan;
  const std::vector<Area>& areas = mesh.areas_;
  const Graph graph{areas, mesh.links_, mesh.out_links_};

  const int from = mesh.FindArea(start);
  if (from < 0) {
    plan.why = "start off the mesh";
    return plan;
  }

  // The heuristic's scale: 1 unless an enabled link costs less than its straight length (a
  // teleporter), then that link's ratio, so A* still finds the cheapest path.
  float h_scale = 1.0f;
  for (const std::vector<int>& out : mesh.out_links_) {
    for (int li : out) {
      const Link& link = mesh.links_[li];
      const float length = Dist(link.takeoff, link.landing);
      if (length > 1e-3f && link.cost < h_scale * length) h_scale = link.cost / length;
    }
  }

  // Where to plan to: the goal itself when it is on the mesh and reachable, otherwise the closest
  // point of the goal's xy among the areas the start can reach.
  int to = mesh.FindArea(goal);
  Vec3 target = goal;
  std::vector<Step> steps;
  bool found = to >= 0 && mesh.Island(to) == mesh.Island(from) &&
               SearchAreas(graph, from, start, to, goal, h_scale, steps);

  if (!found) {
    if (to < 0) {
      plan.why = "goal off the mesh";
    } else if (mesh.Island(to) != mesh.Island(from)) {
      plan.why = Format("goal on island %.0f, start on island %.0f", mesh.Island(to),
                        mesh.Island(from));
    } else {
      plan.why = Format("no path from area %.0f to area %.0f (one-way links)", from, to);
    }

    // The areas reachable from the start over the (one-way) neighbor links and links, breadth
    // first.
    std::vector<char> seen(areas.size(), 0);
    std::vector<int> queue{from};
    seen[from] = 1;
    const auto visit = [&](int v) {
      if (!InRange(v, areas.size()) || seen[v]) return;
      seen[v] = 1;
      queue.push_back(v);
    };
    for (size_t q = 0; q < queue.size(); ++q) {
      const int u = queue[q];
      for (const Edge& e : areas[u].edges) visit(e.neighbor);
      for (int li : mesh.out_links_[u]) visit(mesh.links_[li].to_area);
    }

    // The closest point in xy, ties broken by height.
    float best_xy = std::numeric_limits<float>::infinity();
    float best_dz = std::numeric_limits<float>::infinity();
    for (int a : queue) {
      const Vec3 p = mesh.ClosestPointXY(a, goal.x, goal.y);
      const float d = std::sqrt(DistSqXY(p, goal));
      const float dz = std::fabs(p.z - goal.z);
      if (d < best_xy - kEdgeSlack || (d <= best_xy + kEdgeSlack && dz < best_dz)) {
        best_xy = d;
        best_dz = dz;
        to = a;
        target = p;
      }
    }
    plan.why += Format(" (closest reachable point %.1f away in xy)", best_xy);
    // Reachable by construction; the check keeps a broken mesh from going further.
    if (!SearchAreas(graph, from, start, to, target, h_scale, steps)) return plan;
  }

  // One funnel per walking stretch: from the start (or a landing) over the portals of the shared
  // edges to the next takeoff (or the target). The portals are left and right of a walker going
  // through them; an edge runs counter-clockwise around its area, so the walker leaving across a->b
  // has b on the left.
  const auto add = [&](const Vec3& p, Action action, int link) {
    plan.waypoints.push_back(p);
    plan.actions.push_back(action);
    plan.links.push_back(link);
  };
  Vec3 stretch_start = start;
  std::vector<Portal> portals{{start, start}};
  for (const Step& s : steps) {
    if (s.link < 0) {
      const Edge& e = areas[s.area].edges[s.edge];
      if (mesh.shapes_[s.area].ccw) {
        portals.push_back({e.b, e.a});
      } else {
        portals.push_back({e.a, e.b});
      }
      continue;
    }
    const Link& link = mesh.links_[s.link];
    portals.push_back({link.takeoff, link.takeoff});
    for (const Vec3& p : PullString(portals, stretch_start)) add(p, Action::kWalk, -1);
    add(link.landing, ActionFor(link.kind), link.id);
    stretch_start = link.landing;
    portals.assign(1, Portal{stretch_start, stretch_start});
  }
  portals.push_back({target, target});
  // After a link that lands on the target itself, the last stretch adds nothing.
  const bool landed_on_target =
      portals.size() == 2 && !plan.waypoints.empty() && SamePoint(plan.waypoints.back(), target);
  if (!landed_on_target) {
    for (const Vec3& p : PullString(portals, stretch_start)) add(p, Action::kWalk, -1);
  }
  plan.reached = found;
  return plan;
}

std::vector<Link> GenerateGapLinks(const NavMesh& mesh, const JumpLimits& limits) {
  const std::vector<Area>& areas = mesh.areas_;
  const size_t n = areas.size();
  const float reach = std::max(limits.reach, 0.0f);
  const float up = std::max(limits.up, 0.0f);
  const float down = std::max(limits.down, 0.0f);
  const float min_gap = std::max(limits.min_gap, 0.0f);
  // Two edges whose closest points are this close in xy are stacked: the jump goes straight up or
  // down, and the edges' outward normals decide whether they face each other.
  constexpr float kStackedGap = 1.0f;
  // The jump direction must point at least this much (a cosine, about 75 degrees) out of the first
  // edge and into the second.
  constexpr float kFacing = 0.25f;

  // Islands over the neighbor links only (walking), whatever links the mesh has already.
  std::vector<int> walk_island(n, -1);
  {
    std::vector<std::vector<int>> joined(n);
    for (size_t i = 0; i < n; ++i) {
      for (const Edge& e : areas[i].edges) {
        if (!mesh.ValidNeighbor(int(i), e.neighbor)) continue;
        joined[i].push_back(e.neighbor);
        joined[e.neighbor].push_back(int(i));
      }
    }
    int count = 0;
    std::vector<int> stack;
    for (size_t seed = 0; seed < n; ++seed) {
      if (walk_island[seed] != -1) continue;
      walk_island[seed] = count;
      stack.push_back(int(seed));
      while (!stack.empty()) {
        const int a = stack.back();
        stack.pop_back();
        for (int b : joined[a]) {
          if (walk_island[b] != -1) continue;
          walk_island[b] = count;
          stack.push_back(b);
        }
      }
      ++count;
    }
  }

  // The boundary edges with their outward normals (unit, xy) and boxes.
  struct Boundary {
    int area;
    Vec3 a, b;
    float ox, oy;
    float min_x, min_y, max_x, max_y, min_z, max_z;
  };
  std::vector<Boundary> edges;
  float lo_x = kInf, lo_y = kInf, hi_x = -kInf, hi_y = -kInf;
  for (size_t i = 0; i < n; ++i) {
    const NavMesh::Shape& s = mesh.shapes_[i];
    if (areas[i].polygon.size() >= 3) {
      lo_x = std::min(lo_x, s.min_x);
      lo_y = std::min(lo_y, s.min_y);
      hi_x = std::max(hi_x, s.max_x);
      hi_y = std::max(hi_y, s.max_y);
    }
    for (const Edge& e : areas[i].edges) {
      if (mesh.ValidNeighbor(int(i), e.neighbor)) continue;
      if (!Finite(e.a) || !Finite(e.b)) continue;
      const float dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const float len = std::sqrt(dx * dx + dy * dy);
      if (len < kEdgeSlack) continue;
      // The inside is left of a->b for a counter-clockwise area, so outward is to the right.
      const float ox = s.ccw ? dy / len : -dy / len;
      const float oy = s.ccw ? -dx / len : dx / len;
      edges.push_back({int(i), e.a, e.b, ox, oy, std::min(e.a.x, e.b.x), std::min(e.a.y, e.b.y),
                       std::max(e.a.x, e.b.x), std::max(e.a.y, e.b.y), std::min(e.a.z, e.b.z),
                       std::max(e.a.z, e.b.z)});
    }
  }
  if (edges.empty() || !(lo_x <= hi_x)) return {};

  // The cheapest candidate per (from area, to area); a map keeps the output in a fixed order.
  std::map<std::pair<int, int>, Link> best;
  WalkSearch walk(areas);
  const auto consider = [&](int from, int to, const Vec3& takeoff, const Vec3& landing,
                            LinkKind kind) {
    const float length = Dist(takeoff, landing);
    const float cost = 2.0f * length;
    const auto it = best.find({from, to});
    if (it != best.end() && it->second.cost <= cost) return;
    if (walk_island[from] == walk_island[to]) {
      const float bound = 2.0f * length + min_gap;
      if (walk.Length(from, takeoff, to, landing, bound) <= bound) return;  // just walk
    }
    best[{from, to}] = Link{from, to, takeoff, landing, kind, cost, -1};
  };

  // Rule 1: edge pairs, each edge against the edges within reach of its box.
  Grid2D edge_grid(lo_x, lo_y, hi_x, hi_y, std::max(reach, 4.0f * min_gap));
  for (size_t k = 0; k < edges.size(); ++k) {
    edge_grid.Insert(int(k), edges[k].min_x, edges[k].min_y, edges[k].max_x, edges[k].max_y);
  }
  std::vector<int> stamp(edges.size(), -1);
  std::vector<int> near;
  for (size_t i = 0; i < edges.size(); ++i) {
    const Boundary& e1 = edges[i];
    near.clear();
    edge_grid.Query(e1.min_x - reach, e1.min_y - reach, e1.max_x + reach, e1.max_y + reach,
                    [&](int j) {
                      if (stamp[j] == int(i)) return;
                      stamp[j] = int(i);
                      near.push_back(j);
                    });
    for (int j : near) {
      const Boundary& e2 = edges[j];
      if (e2.area == e1.area) continue;
      if (e2.min_z - e1.max_z > up || e1.min_z - e2.max_z > down) continue;
      float s, t;
      ClosestParamsXY(e1.a, e1.b, e2.a, e2.b, s, t);
      const Vec3 takeoff = Lerp(e1.a, e1.b, s);
      const Vec3 q = Lerp(e2.a, e2.b, t);
      const float gx = q.x - takeoff.x, gy = q.y - takeoff.y;
      const float gap = std::sqrt(gx * gx + gy * gy);
      const float dz = q.z - takeoff.z;
      if (gap > reach || dz > up || dz < -down) continue;
      if (gap * gap + dz * dz < min_gap * min_gap) continue;  // the edges touch
      if (gap >= kStackedGap) {
        const float ux = gx / gap, uy = gy / gap;
        if (ux * e1.ox + uy * e1.oy < kFacing) continue;   // does not leave across e1
        if (ux * e2.ox + uy * e2.oy > -kFacing) continue;  // does not enter across e2
      } else if (e1.ox * e2.ox + e1.oy * e2.oy > -0.5f) {
        continue;  // stacked but not facing: one floor over the other
      }
      const Vec3 landing =
          mesh.ClosestPointXY(e2.area, q.x - e2.ox * min_gap, q.y - e2.oy * min_gap);
      // A walk-off only when the fall carries the walker across the gap (the header comment):
      // reach / 4 * sqrt(drop / up), at most reach / 3; a gap of 2 * min_gap always.
      const float walk_off =
          up > 0.0f && dz < 0.0f ? std::min(reach / 3.0f, 0.25f * reach * std::sqrt(-dz / up))
                                 : 0.0f;
      const bool walks_off = gap <= 2.0f * min_gap || gap <= walk_off;
      const LinkKind kind = dz <= -min_gap && walks_off ? LinkKind::kDrop : LinkKind::kJump;
      consider(e1.area, e2.area, takeoff, landing, kind);
    }
  }

  // Rule 2: floors below, probed straight out from each boundary edge.
  Grid2D area_grid(lo_x, lo_y, hi_x, hi_y, std::max(reach, 4.0f * min_gap));
  for (size_t i = 0; i < n; ++i) {
    if (areas[i].polygon.size() < 3) continue;
    const NavMesh::Shape& s = mesh.shapes_[i];
    area_grid.Insert(int(i), s.min_x, s.min_y, s.max_x, s.max_y);
  }
  std::vector<int> area_stamp(n, -1);
  int probe_id = 0;
  const float out = 2.0f * min_gap;
  for (const Boundary& e : edges) {
    for (const float along : {0.25f, 0.5f, 0.75f}) {
      const Vec3 takeoff = Lerp(e.a, e.b, along);
      const float px = takeoff.x + e.ox * out, py = takeoff.y + e.oy * out;
      int below = -1;
      float below_z = -kInf;
      bool level = false;  // a floor at about the edge's height out there: not a drop
      const int id = probe_id++;
      area_grid.Query(px, py, px, py, [&](int c) {
        if (area_stamp[c] == id || c == e.area) return;
        area_stamp[c] = id;
        if (!mesh.ContainsXY(c, px, py)) return;
        const float z = mesh.FloorZ(c, px, py);
        const float dz = z - takeoff.z;
        if (std::fabs(dz) < min_gap) {
          level = true;
        } else if (dz < 0.0f && z > below_z) {
          below = c;
          below_z = z;
        }
      });
      if (level || below < 0 || takeoff.z - below_z > down) continue;
      // Land well inside the floor, not on its rim.
      const Vec3 landing{px, py, below_z};
      bool rim = false;
      for (const Edge& be : areas[below].edges) {
        if (mesh.ValidNeighbor(below, be.neighbor)) continue;
        if (SegmentDistXY(landing, be.a, be.b) < 0.5f * min_gap) rim = true;
      }
      if (!rim) consider(e.area, below, takeoff, landing, LinkKind::kDrop);
    }
  }

  std::vector<Link> result;
  result.reserve(best.size());
  int next_id = mesh.next_link_id_;
  for (auto& [key, link] : best) {
    link.id = next_id++;
    result.push_back(link);
  }
  return result;
}

}  // namespace skylanders::nav
