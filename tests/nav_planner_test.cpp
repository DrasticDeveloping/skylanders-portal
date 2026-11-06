// The navmesh planner (nav/nav_planner.h) on hand-built meshes of squares. No game data: every
// mesh here is made up in the test.

#include <algorithm>
#include <cmath>
#include <deque>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "nav/nav_planner.h"
#include "test_util.h"

using namespace skylanders::nav;

namespace {

constexpr float S = 100.0f;  // the side of a square, in world units

using Cell = std::pair<int, int>;  // (column i, row j): the square [i*S, (i+1)*S] x [j*S, (j+1)*S]

// One square area per cell, at height z, its edges linked to the cells beside it (4-neighbours).
// The polygon runs counter-clockwise; edge k goes from vertex k to vertex k+1.
std::vector<Area> Squares(const std::vector<Cell>& cells, float z = 0.0f) {
  std::map<Cell, int> index;
  for (size_t k = 0; k < cells.size(); ++k) index[cells[k]] = int(k);
  const auto at = [&](int i, int j) {
    auto it = index.find({i, j});
    return it == index.end() ? -1 : it->second;
  };
  std::vector<Area> areas;
  for (const auto& [i, j] : cells) {
    const float x0 = i * S, x1 = (i + 1) * S, y0 = j * S, y1 = (j + 1) * S;
    const Vec3 v[4] = {{x0, y0, z}, {x1, y0, z}, {x1, y1, z}, {x0, y1, z}};
    const int across[4] = {at(i, j - 1), at(i + 1, j), at(i, j + 1), at(i - 1, j)};
    Area a;
    for (int k = 0; k < 4; ++k) {
      a.polygon.push_back(v[k]);
      a.edges.push_back({across[k], v[k], v[(k + 1) % 4]});
    }
    a.center = {(x0 + x1) / 2, (y0 + y1) / 2, z};
    areas.push_back(std::move(a));
  }
  return areas;
}

Vec3 Center(int i, int j, float z = 0.0f) { return {(i + 0.5f) * S, (j + 0.5f) * S, z}; }

bool Near(const Vec3& p, float x, float y, float z = 0.0f, float eps = 0.01f) {
  return std::fabs(p.x - x) <= eps && std::fabs(p.y - y) <= eps && std::fabs(p.z - z) <= eps;
}
bool Near(const Vec3& p, const Vec3& q) { return Near(p, q.x, q.y, q.z); }

float Length(const Vec3& start, const std::vector<Vec3>& waypoints) {
  float total = 0.0f;
  Vec3 prev = start;
  for (const Vec3& w : waypoints) {
    total += std::hypot(w.x - prev.x, w.y - prev.y, w.z - prev.z);
    prev = w;
  }
  return total;
}

// Every point of the polyline start -> waypoints (sampled every S/20) is on some area.
bool Inside(const NavMesh& mesh, const Vec3& start, const std::vector<Vec3>& waypoints) {
  Vec3 prev = start;
  for (const Vec3& w : waypoints) {
    const float len = std::hypot(w.x - prev.x, w.y - prev.y);
    const int samples = int(len / (S / 20)) + 1;
    for (int k = 0; k <= samples; ++k) {
      const float t = float(k) / samples;
      const Vec3 p{prev.x + t * (w.x - prev.x), prev.y + t * (w.y - prev.y),
                   prev.z + t * (w.z - prev.z)};
      if (mesh.FindArea(p) < 0) return false;
    }
    prev = w;
  }
  return true;
}

// The cells of a w x h grid minus the holes.
std::vector<Cell> Grid(int w, int h, const std::set<Cell>& holes = {}) {
  std::vector<Cell> cells;
  for (int j = 0; j < h; ++j) {
    for (int i = 0; i < w; ++i) {
      if (!holes.count({i, j})) cells.push_back({i, j});
    }
  }
  return cells;
}

// Steps of the shortest 4-neighbour walk between two free cells, -1 when there is none.
int GridSteps(const std::vector<Cell>& cells, Cell from, Cell to) {
  const std::set<Cell> free(cells.begin(), cells.end());
  std::map<Cell, int> dist{{from, 0}};
  std::deque<Cell> queue{from};
  while (!queue.empty()) {
    const Cell c = queue.front();
    queue.pop_front();
    if (c == to) return dist[c];
    const Cell next[4] = {{c.first + 1, c.second},
                          {c.first - 1, c.second},
                          {c.first, c.second + 1},
                          {c.first, c.second - 1}};
    for (const Cell& n : next) {
      if (!free.count(n) || dist.count(n)) continue;
      dist[n] = dist[c] + 1;
      queue.push_back(n);
    }
  }
  return -1;
}

// The areas moved by (dx, 0, dz); neighbor indices stay as they are.
std::vector<Area> Shift(std::vector<Area> areas, float dx, float dz) {
  const auto move = [&](Vec3& v) {
    v.x += dx;
    v.z += dz;
  };
  for (Area& a : areas) {
    for (Vec3& v : a.polygon) move(v);
    for (Edge& e : a.edges) {
      move(e.a);
      move(e.b);
    }
    move(a.center);
  }
  return areas;
}

// a then b, b's neighbor indices moved past a's areas. The two parts share no link.
std::vector<Area> Join(std::vector<Area> a, const std::vector<Area>& b) {
  const int base = int(a.size());
  for (Area area : b) {
    for (Edge& e : area.edges) {
      if (e.neighbor >= 0) e.neighbor += base;
    }
    a.push_back(std::move(area));
  }
  return a;
}

// The row-0 corridor of the cells first..last (inclusive).
std::vector<Cell> Row(int first, int last) {
  std::vector<Cell> cells;
  for (int i = first; i <= last; ++i) cells.push_back({i, 0});
  return cells;
}

// actions and links run beside the waypoints, a link's landing comes right after a kWalk takeoff,
// and only landings carry a link id.
bool Parallel(const Plan& plan) {
  if (plan.actions.size() != plan.waypoints.size()) return false;
  if (plan.links.size() != plan.waypoints.size()) return false;
  for (size_t k = 0; k < plan.actions.size(); ++k) {
    const bool walk = plan.actions[k] == Action::kWalk;
    if (walk != (plan.links[k] == -1)) return false;
    if (!walk && (k == 0 || plan.actions[k - 1] != Action::kWalk)) return false;
  }
  return true;
}

// The one link of `links` from area `from` to area `to`; nullptr when there is none or several.
const Link* Find(const std::vector<Link>& links, int from, int to) {
  const Link* found = nullptr;
  for (const Link& l : links) {
    if (l.from_area != from || l.to_area != to) continue;
    if (found) return nullptr;
    found = &l;
  }
  return found;
}

// The index of a waypoint whose link is `id`, -1 when the plan does not use it.
int Uses(const Plan& plan, int id) {
  const auto it = std::find(plan.links.begin(), plan.links.end(), id);
  return it == plan.links.end() ? -1 : int(it - plan.links.begin());
}

}  // namespace

int main() {
  // A straight corridor of ten squares: the funnel leaves only the goal.
  {
    std::vector<Cell> cells;
    for (int i = 0; i < 10; ++i) cells.push_back({i, 0});
    const NavMesh mesh(Squares(cells));
    CHECK(mesh.AreaCount() == 10);
    CHECK(mesh.IslandCount() == 1);
    CHECK(mesh.Symmetric());
    CHECK(mesh.FindArea(Center(3, 0)) == 3);
    CHECK(mesh.FindArea({-1.0f, 50.0f, 0.0f}) == -1);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(9, 0));
    CHECK(plan.reached);
    CHECK(plan.why.empty());
    REQUIRE_OR_RETURN(plan.waypoints.size() == 1);
    CHECK(Near(plan.waypoints[0], Center(9, 0)));

    // Off the centre line it is still one straight segment.
    const Plan slant = PlanPath(mesh, {10.0f, 10.0f, 0.0f}, {990.0f, 90.0f, 0.0f});
    CHECK(slant.reached && slant.waypoints.size() == 1);

    // Start and goal in the same area: the goal alone, also when they are the same point.
    const Plan same = PlanPath(mesh, Center(4, 0), {420.0f, 30.0f, 0.0f});
    CHECK(same.reached && same.waypoints.size() == 1 && Near(same.waypoints[0], 420, 30));
    const Plan here = PlanPath(mesh, Center(4, 0), Center(4, 0));
    CHECK(here.reached && here.waypoints.size() == 1 && Near(here.waypoints[0], Center(4, 0)));
  }

  // An L: five squares right, then four up. One corner, at the inner corner (4S, S).
  {
    std::vector<Cell> cells;
    for (int i = 0; i < 5; ++i) cells.push_back({i, 0});
    for (int j = 1; j < 5; ++j) cells.push_back({4, j});
    const NavMesh mesh(Squares(cells));
    CHECK(mesh.Symmetric() && mesh.IslandCount() == 1);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(4, 4));
    CHECK(plan.reached);
    REQUIRE_OR_RETURN(plan.waypoints.size() == 2);
    CHECK(Near(plan.waypoints[0], 4 * S, 1 * S));
    CHECK(Near(plan.waypoints[1], Center(4, 4)));
    CHECK(Inside(mesh, Center(0, 0), plan.waypoints));

    // And back the other way, the same corner.
    const Plan back = PlanPath(mesh, Center(4, 4), Center(0, 0));
    CHECK(back.reached && back.waypoints.size() == 2);
    if (back.waypoints.size() == 2) CHECK(Near(back.waypoints[0], 4 * S, 1 * S));
  }

  // A U-turn: right along row 0, up through (4,1), left along row 2. Rows 0 and 2 touch nowhere
  // else. Two corners, at (4S, S) and (4S, 2S).
  {
    std::vector<Cell> cells;
    for (int i = 0; i < 5; ++i) cells.push_back({i, 0});
    cells.push_back({4, 1});
    for (int i = 0; i < 5; ++i) cells.push_back({i, 2});
    const NavMesh mesh(Squares(cells));
    CHECK(mesh.Symmetric() && mesh.IslandCount() == 1);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(0, 2));
    CHECK(plan.reached);
    REQUIRE_OR_RETURN(plan.waypoints.size() == 3);
    CHECK(Near(plan.waypoints[0], 4 * S, 1 * S));
    CHECK(Near(plan.waypoints[1], 4 * S, 2 * S));
    CHECK(Near(plan.waypoints[2], Center(0, 2)));
    CHECK(Inside(mesh, Center(0, 0), plan.waypoints));
    // The way round is 4 + 1 + 4 squares long between the corners and the ends, give or take.
    const float expect = std::hypot(3.5f * S, 0.5f * S) + S + std::hypot(3.5f * S, 0.5f * S);
    CHECK(std::fabs(Length(Center(0, 0), plan.waypoints) - expect) < 0.1f);
  }

  // Two islands: squares 0..2 and 5..7 of a row. The plan is not reached, says why, and goes to the
  // closest point of the goal's xy on the start's island, the end of square 2.
  {
    const NavMesh mesh(Squares({{0, 0}, {1, 0}, {2, 0}, {5, 0}, {6, 0}, {7, 0}}));
    CHECK(mesh.IslandCount() == 2);
    CHECK(mesh.Island(0) == mesh.Island(2));
    CHECK(mesh.Island(0) != mesh.Island(3));
    CHECK(mesh.Island(-1) == -1 && mesh.Island(6) == -1);
    CHECK(mesh.Symmetric());
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(5, 0));
    CHECK(!plan.reached);
    CHECK(!plan.why.empty());
    CHECK(plan.why.find("island") != std::string::npos);
    REQUIRE_OR_RETURN(!plan.waypoints.empty());
    CHECK(Near(plan.waypoints.back(), 3 * S, 0.5f * S));
  }

  // Start or goal off the mesh.
  {
    std::vector<Cell> cells;
    for (int i = 0; i < 10; ++i) cells.push_back({i, 0});
    const NavMesh mesh(Squares(cells));

    // Start off: no plan at all.
    const Plan off_start = PlanPath(mesh, {-5 * S, 0.5f * S, 0.0f}, Center(5, 0));
    CHECK(!off_start.reached && !off_start.why.empty() && off_start.waypoints.empty());
    // Start above the mesh by more than FindArea's reach: also off.
    const Plan high_start = PlanPath(mesh, Center(0, 0, 1000.0f), Center(5, 0));
    CHECK(!high_start.reached && high_start.waypoints.empty());

    // Goal beside the corridor: the plan goes to the nearest point of it, on the edge of square 5.
    const Plan off_goal = PlanPath(mesh, Center(0, 0), {5.5f * S, 3 * S, 0.0f});
    CHECK(!off_goal.reached);
    CHECK(off_goal.why.find("off the mesh") != std::string::npos);
    REQUIRE_OR_RETURN(!off_goal.waypoints.empty());
    CHECK(Near(off_goal.waypoints.back(), 5.5f * S, 1 * S));
    CHECK(Inside(mesh, Center(0, 0), off_goal.waypoints));

    // Goal far above the floor: off the mesh too; the plan ends under it.
    const Plan high_goal = PlanPath(mesh, Center(0, 0), Center(5, 0, 1000.0f));
    CHECK(!high_goal.reached && !high_goal.why.empty());
    REQUIRE_OR_RETURN(!high_goal.waypoints.empty());
    CHECK(Near(high_goal.waypoints.back(), Center(5, 0)));
  }

  // One-way links: Symmetric() finds them, the island stays one, and A* follows links only the way
  // they point.
  {
    std::vector<Area> areas = Squares({{0, 0}, {1, 0}, {2, 0}});
    CHECK(NavMesh(areas).Symmetric());
    for (Edge& e : areas[1].edges) {
      if (e.neighbor == 2) e.neighbor = -1;  // 1 no longer leads to 2; 2 still leads to 1
    }
    const NavMesh mesh(areas);
    CHECK(!mesh.Symmetric());
    CHECK(mesh.IslandCount() == 1);
    const Plan forward = PlanPath(mesh, Center(0, 0), Center(2, 0));
    CHECK(!forward.reached && !forward.why.empty());
    REQUIRE_OR_RETURN(!forward.waypoints.empty());
    CHECK(Near(forward.waypoints.back(), 2 * S, 0.5f * S));  // the end of square 1
    const Plan backward = PlanPath(mesh, Center(2, 0), Center(0, 0));
    CHECK(backward.reached && backward.waypoints.size() == 1);

    // A link to an area that does not exist has no reverse either, and joins nothing.
    std::vector<Area> broken = Squares({{0, 0}, {1, 0}});
    broken[0].edges[0].neighbor = 99;
    const NavMesh bad(broken);
    CHECK(!bad.Symmetric());
    CHECK(bad.IslandCount() == 1);
    CHECK(PlanPath(bad, Center(0, 0), Center(1, 0)).reached);
  }

  // FindArea picks the floor nearest in z: two squares stacked 100 apart, not linked.
  {
    std::vector<Area> areas = Squares({{0, 0}});
    std::vector<Area> upper = Squares({{0, 0}}, 100.0f);
    areas.push_back(upper[0]);
    const NavMesh mesh(areas);
    CHECK(mesh.IslandCount() == 2);
    CHECK(mesh.FindArea(Center(0, 0, 10.0f)) == 0);
    CHECK(mesh.FindArea(Center(0, 0, 90.0f)) == 1);
    CHECK(mesh.FindArea(Center(0, 0, 250.0f)) == 1);
    CHECK(mesh.FindArea(Center(0, 0, 500.0f)) == -1);
    CHECK(mesh.FindArea(Center(0, 0, 130.0f), 20.0f) == -1);
    // Edges and corners count as inside.
    CHECK(mesh.FindArea({S, S, 0.0f}) == 0);
    CHECK(mesh.FindArea({0.0f, 0.5f * S, 100.0f}) == 1);
  }

  // A ramp: the floor height comes from the polygon's plane; a clockwise polygon works too.
  {
    Area ramp;
    ramp.polygon = {{0, 0, 0}, {10 * S, 0, 0}, {10 * S, 10 * S, 500}, {0, 10 * S, 500}};
    ramp.center = {5 * S, 5 * S, 250};
    for (int k = 0; k < 4; ++k) {
      ramp.edges.push_back({-1, ramp.polygon[k], ramp.polygon[(k + 1) % 4]});
    }
    Area flipped = ramp;
    flipped.polygon = {ramp.polygon[3], ramp.polygon[2], ramp.polygon[1], ramp.polygon[0]};
    flipped.center.x += 20 * S;
    for (Vec3& v : flipped.polygon) v.x += 20 * S;
    flipped.edges.clear();
    for (int k = 0; k < 4; ++k) {
      flipped.edges.push_back({-1, flipped.polygon[k], flipped.polygon[(k + 1) % 4]});
    }
    const NavMesh mesh({ramp, flipped});
    CHECK(mesh.FindArea({5 * S, 5 * S, 250.0f}, 1.0f) == 0);
    CHECK(mesh.FindArea({5 * S, 2 * S, 100.0f}, 1.0f) == 0);
    CHECK(mesh.FindArea({5 * S, 5 * S, 300.0f}, 1.0f) == -1);
    CHECK(mesh.FindArea({25 * S, 5 * S, 250.0f}, 1.0f) == 1);
    CHECK(mesh.FindArea({25 * S, 11 * S, 250.0f}) == -1);
  }

  // An open 10x10 grid: corner to corner is one straight diagonal (through the grid's vertices).
  {
    const std::vector<Cell> cells = Grid(10, 10);
    const NavMesh mesh(Squares(cells));
    CHECK(mesh.Symmetric() && mesh.IslandCount() == 1);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(9, 9));
    CHECK(plan.reached);
    REQUIRE_OR_RETURN(plan.waypoints.size() == 1);
    CHECK(Near(plan.waypoints[0], Center(9, 9)));
  }

  // A 10x10 grid with a wall at column 5, open only in the top row: the path goes through the gap
  // and turns at both bottom corners of it, (5S, 9S) and (6S, 9S).
  {
    std::set<Cell> wall;
    for (int j = 0; j < 9; ++j) wall.insert({5, j});
    const std::vector<Cell> cells = Grid(10, 10, wall);
    const NavMesh mesh(Squares(cells));
    const Vec3 start = Center(2, 2), goal = Center(8, 2);
    const Plan plan = PlanPath(mesh, start, goal);
    CHECK(plan.reached);
    CHECK(Inside(mesh, start, plan.waypoints));
    REQUIRE_OR_RETURN(plan.waypoints.size() >= 3);
    CHECK(Near(plan.waypoints.back(), goal));
    bool gap = false;
    for (size_t k = 0; k + 1 < plan.waypoints.size(); ++k) {
      gap |= Near(plan.waypoints[k], 5 * S, 9 * S) && Near(plan.waypoints[k + 1], 6 * S, 9 * S);
    }
    CHECK(gap);
    const float best = std::hypot(2.5f * S, 6.5f * S) * 2 + S;
    const float length = Length(start, plan.waypoints);
    CHECK(length >= best - 0.1f);
    CHECK(length <= best * 1.25f);
  }

  // A 30x30 grid with holes (walls with gaps and a block): a path exists, follows the gaps, stays
  // on the areas, and is no longer than the 4-neighbour walk.
  {
    std::set<Cell> holes;
    for (int j = 0; j <= 24; ++j) holes.insert({5, j});    // open at the top (rows 25..29)
    for (int j = 5; j <= 29; ++j) holes.insert({12, j});   // open at the bottom (rows 0..4)
    for (int i = 18; i <= 22; ++i) {
      for (int j = 10; j <= 20; ++j) holes.insert({i, j});  // a block
    }
    for (int i = 13; i <= 27; ++i) holes.insert({i, 24});  // open at the right (columns 28, 29)
    const std::vector<Cell> cells = Grid(30, 30, holes);
    const NavMesh mesh(Squares(cells));
    CHECK(mesh.AreaCount() == cells.size());
    CHECK(mesh.Symmetric());
    CHECK(mesh.IslandCount() == 1);
    const int steps = GridSteps(cells, {0, 0}, {29, 29});
    REQUIRE_OR_RETURN(steps > 0);

    const Vec3 start = Center(0, 0), goal = Center(29, 29);
    const Plan plan = PlanPath(mesh, start, goal);
    CHECK(plan.reached);
    CHECK(plan.why.empty());
    REQUIRE_OR_RETURN(plan.waypoints.size() >= 2);
    CHECK(Near(plan.waypoints.back(), goal));
    CHECK(Inside(mesh, start, plan.waypoints));
    // Every corner is a vertex of the grid.
    for (size_t k = 0; k + 1 < plan.waypoints.size(); ++k) {
      const Vec3& w = plan.waypoints[k];
      CHECK(std::fabs(w.x / S - std::round(w.x / S)) < 1e-3f);
      CHECK(std::fabs(w.y / S - std::round(w.y / S)) < 1e-3f);
    }
    const float length = Length(start, plan.waypoints);
    CHECK(length >= std::hypot(goal.x - start.x, goal.y - start.y));
    CHECK(length <= steps * S);
    // The gaps are on the way: the path crosses the middle line of wall 5 above it, of wall 12 below
    // it and of the row-24 wall right of it. (A waypoint need not sit on a gap's corner: A* picks
    // one of several equal-cost channels through the grid, and a straight run up column 29 rounds
    // the row-24 wall without one.)
    bool over5 = false, under12 = false, round24 = false;
    Vec3 prev = start;
    for (const Vec3& w : plan.waypoints) {
      const auto cross = [&](float a0, float a1, float b0, float b1, float line, float* at) {
        if ((a0 - line) * (a1 - line) > 0.0f || a0 == a1) return false;
        *at = b0 + (line - a0) / (a1 - a0) * (b1 - b0);
        return true;
      };
      float at = 0.0f;
      if (cross(prev.x, w.x, prev.y, w.y, 5.5f * S, &at)) over5 |= at >= 25 * S - 0.01f;
      if (cross(prev.x, w.x, prev.y, w.y, 12.5f * S, &at)) under12 |= at <= 5 * S + 0.01f;
      if (cross(prev.y, w.y, prev.x, w.x, 24.5f * S, &at)) round24 |= at >= 28 * S - 0.01f;
      prev = w;
    }
    CHECK(over5 && under12 && round24);
    if (!(over5 && under12 && round24)) {
      for (const Vec3& w : plan.waypoints) std::printf("  waypoint %.1f %.1f\n", w.x / S, w.y / S);
    }

    // From a hole the start is off the mesh.
    CHECK(!PlanPath(mesh, Center(20, 15), goal).reached);
  }

  // ---- Links: jumps, drops and the game's own links. ----
  const JumpLimits limits;  // reach 300, up 100, down 600, min_gap 20

  // A plan without links has every waypoint kWalk with link -1.
  {
    const NavMesh mesh(Squares(Row(0, 4)));
    CHECK(mesh.Links().empty());
    CHECK(GenerateGapLinks(mesh, limits).empty());
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(4, 0));
    CHECK(plan.reached && Parallel(plan));
    CHECK(plan.actions.size() == 1 && plan.actions[0] == Action::kWalk && plan.links[0] == -1);
  }

  // A gap of 2S (within reach) between two corridors: one jump each way, from the middle of the
  // facing edges, the landing min_gap inside. The plan walks to the takeoff and jumps.
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Squares(Row(5, 7))));
    CHECK(mesh.IslandCount() == 2);
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 2);
    const Link* there = Find(links, 2, 3);
    const Link* back = Find(links, 3, 2);
    REQUIRE_OR_RETURN(there && back);
    CHECK(there->kind == LinkKind::kJump && back->kind == LinkKind::kJump);
    CHECK(Near(there->takeoff, 3 * S, 0.5f * S));
    CHECK(Near(there->landing, 5 * S + limits.min_gap, 0.5f * S));
    CHECK(Near(back->takeoff, 5 * S, 0.5f * S));
    CHECK(Near(back->landing, 3 * S - limits.min_gap, 0.5f * S));
    CHECK(there->id != back->id);
    CHECK(std::fabs(there->cost - 2 * (2 * S + limits.min_gap)) < 0.01f);

    // Before the links the goal is on another island.
    CHECK(!PlanPath(mesh, Center(0, 0), Center(7, 0)).reached);

    mesh.AddLinks(links);
    CHECK(mesh.Links().size() == 2);
    CHECK(mesh.IslandCount() == 1);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(7, 0));
    CHECK(plan.reached && plan.why.empty() && Parallel(plan));
    REQUIRE_OR_RETURN(plan.waypoints.size() == 3);
    CHECK(Near(plan.waypoints[0], 3 * S, 0.5f * S) && plan.actions[0] == Action::kWalk);
    CHECK(Near(plan.waypoints[1], there->landing) && plan.actions[1] == Action::kJump);
    CHECK(plan.links[1] == there->id);
    CHECK(Near(plan.waypoints[2], Center(7, 0)) && plan.actions[2] == Action::kWalk);

    // The same link fails in practice and is disabled: the way back still joins the islands, but
    // nothing leads there any more.
    CHECK(mesh.DisableLink(there->id));
    CHECK(mesh.LinkDisabled(there->id) && !mesh.LinkDisabled(back->id));
    CHECK(!mesh.DisableLink(12345) && !mesh.LinkDisabled(12345));
    CHECK(mesh.Links().size() == 2);  // still listed
    CHECK(mesh.IslandCount() == 1);
    const Plan failed = PlanPath(mesh, Center(0, 0), Center(7, 0));
    CHECK(!failed.reached && !failed.why.empty() && Parallel(failed));
    CHECK(Uses(failed, there->id) == -1);
    REQUIRE_OR_RETURN(!failed.waypoints.empty());
    CHECK(Near(failed.waypoints.back(), 3 * S, 0.5f * S));
    // The other way still jumps.
    const Plan home = PlanPath(mesh, Center(7, 0), Center(0, 0));
    CHECK(home.reached && Uses(home, back->id) == 1);
    // With both disabled the islands are apart again.
    CHECK(mesh.DisableLink(back->id));
    CHECK(mesh.IslandCount() == 2);
    CHECK(PlanPath(mesh, Center(0, 0), Center(7, 0)).why.find("island") != std::string::npos);
  }

  // A gap of 4S, beyond reach: no link, the goal stays on another island.
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Squares(Row(7, 9))));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.empty());
    mesh.AddLinks(links);
    CHECK(mesh.IslandCount() == 2);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(8, 0));
    CHECK(!plan.reached && plan.why.find("island") != std::string::npos && Parallel(plan));
    // A shorter reach than the gap of 2S finds nothing either.
    JumpLimits short_reach = limits;
    short_reach.reach = 150;
    CHECK(GenerateGapLinks(NavMesh(Join(Squares(Row(0, 2)), Squares(Row(5, 7)))), short_reach)
              .empty());
  }

  // A ledge 80 up (within up) behind a gap of 50: a jump up, and a drop (walk off) back down.
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 50, 80)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 2);
    const Link* up = Find(links, 2, 3);
    const Link* down = Find(links, 3, 2);
    REQUIRE_OR_RETURN(up && down);
    CHECK(up->kind == LinkKind::kJump);
    CHECK(Near(up->takeoff, 3 * S, 0.5f * S, 0.0f));
    CHECK(Near(up->landing, 3 * S + 50 + limits.min_gap, 0.5f * S, 80.0f));
    CHECK(down->kind == LinkKind::kDrop);
    CHECK(Near(down->landing, 3 * S - limits.min_gap, 0.5f * S, 0.0f));
    mesh.AddLinks(links);
    const Plan plan = PlanPath(mesh, Center(0, 0), {4.5f * S + 50, 0.5f * S, 80.0f});
    CHECK(plan.reached && Parallel(plan));
    REQUIRE_OR_RETURN(plan.waypoints.size() == 3);
    CHECK(plan.actions[1] == Action::kJump && plan.links[1] == up->id);
    const Plan back = PlanPath(mesh, {4.5f * S + 50, 0.5f * S, 80.0f}, Center(0, 0));
    CHECK(back.reached && Parallel(back));
    REQUIRE_OR_RETURN(back.waypoints.size() == 3);
    CHECK(back.actions[1] == Action::kDrop && back.links[1] == down->id);
  }

  // A ledge 150 up, above up: no way up, only the drop down. Up there is unreachable from below
  // (one island, joined by the drop, but no path).
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 50, 150)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 1);
    CHECK(!Find(links, 2, 3));
    const Link* down = Find(links, 3, 2);
    REQUIRE_OR_RETURN(down);
    CHECK(down->kind == LinkKind::kDrop);
    mesh.AddLinks(links);
    CHECK(mesh.IslandCount() == 1);
    const Plan plan = PlanPath(mesh, Center(0, 0), {4.5f * S + 50, 0.5f * S, 150.0f});
    CHECK(!plan.reached && plan.why.find("no path") != std::string::npos && Parallel(plan));
    REQUIRE_OR_RETURN(!plan.waypoints.empty());
    CHECK(Near(plan.waypoints.back(), 3 * S, 0.5f * S));
  }

  // A drop of 300 (within down) across a gap of 40: a drop, and no jump back up.
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 40, 300)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 1);
    const Link* down = Find(links, 3, 2);
    REQUIRE_OR_RETURN(down);
    CHECK(down->kind == LinkKind::kDrop);
    CHECK(Near(down->takeoff, 3 * S + 40, 0.5f * S, 300.0f));
    CHECK(Near(down->landing, 3 * S - limits.min_gap, 0.5f * S, 0.0f));
    mesh.AddLinks(links);
    const Vec3 top{4.5f * S + 40, 0.5f * S, 300.0f};
    const Plan plan = PlanPath(mesh, top, Center(0, 0));
    CHECK(plan.reached && Parallel(plan));
    REQUIRE_OR_RETURN(plan.waypoints.size() == 3);
    CHECK(plan.actions[0] == Action::kWalk && Near(plan.waypoints[0], down->takeoff));
    CHECK(plan.actions[1] == Action::kDrop && plan.links[1] == down->id);
    CHECK(!PlanPath(mesh, Center(0, 0), top).reached);
  }

  // A drop of 700, beyond down: nothing either way.
  {
    const NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 40, 700)));
    CHECK(GenerateGapLinks(mesh, limits).empty());
  }

  // A drop of 25 across a gap of 70: walking off falls 25 long before it is across (the walk-off
  // reach is 300 / 4 * sqrt(25 / 100) = 37.5), so it is a jump, not a drop. A jump back up too.
  {
    const NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 70, -25)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 2);
    const Link* across = Find(links, 2, 3);
    const Link* back = Find(links, 3, 2);
    REQUIRE_OR_RETURN(across && back);
    CHECK(across->kind == LinkKind::kJump);
    CHECK(Near(across->takeoff, 3 * S, 0.5f * S, 0.0f));
    CHECK(Near(across->landing, 3 * S + 70 + limits.min_gap, 0.5f * S, -25.0f));
    CHECK(back->kind == LinkKind::kJump);
  }

  // The same gap of 70 with a drop of 300: the fall is long enough to carry the walker across
  // (the walk-off reach is 129.9, capped at reach / 3 = 100), so a drop; nothing back up.
  {
    const NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 70, -300)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    CHECK(links.size() == 1);
    const Link* down = Find(links, 2, 3);
    REQUIRE_OR_RETURN(down);
    CHECK(down->kind == LinkKind::kDrop);
  }

  // A drop of 25 across a gap of 30 (at most 2 * min_gap): a drop whatever its height; the way
  // back up is a jump.
  {
    const NavMesh mesh(Join(Squares(Row(0, 2)), Shift(Squares(Row(3, 5)), 30, -25)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    const Link* down = Find(links, 2, 3);
    const Link* up = Find(links, 3, 2);
    REQUIRE_OR_RETURN(down && up);
    CHECK(down->kind == LinkKind::kDrop);
    CHECK(up->kind == LinkKind::kJump);
  }

  // Walking lengths: through the midpoints of the shared edges, bounded, and none across a gap.
  {
    const NavMesh corridor(Squares(Row(0, 4)));
    const float along = corridor.WalkLength(0, Center(0, 0), 4, Center(4, 0), 1000.0f);
    CHECK(std::fabs(along - 4 * S) < 0.01f);
    CHECK(std::isinf(corridor.WalkLength(0, Center(0, 0), 4, Center(4, 0), 3 * S)));
    CHECK(std::fabs(corridor.WalkLength(1, {120, 50, 0}, 1, {150, 50, 0}, 100.0f) - 30.0f) < 0.01f);
    CHECK(std::isinf(corridor.WalkLength(1, {120, 50, 0}, 1, {150, 50, 0}, 20.0f)));
    CHECK(std::isinf(corridor.WalkLength(0, Center(0, 0), 9, Center(4, 0), 1000.0f)));
    CHECK(std::isinf(corridor.WalkLength(-1, Center(0, 0), 4, Center(4, 0), 1000.0f)));
    NavMesh apart(Join(Squares(Row(0, 2)), Squares(Row(5, 7))));
    CHECK(std::isinf(apart.WalkLength(2, Center(2, 0), 3, Center(5, 0), 1e6f)));
    // Links do not count: a jump across the gap leaves the walk as it was.
    apart.AddLinks(GenerateGapLinks(apart, limits));
    CHECK(apart.IslandCount() == 1);
    CHECK(std::isinf(apart.WalkLength(2, Center(2, 0), 3, Center(5, 0), 1e6f)));
  }

  // Shortest walks: along the walk itself, not through the midpoints. Two areas 200 wide share
  // one long edge (y = 100): a step of 10 across it near one end is 10, where the midpoints make it
  // 180 (WalkLength). Round an L's inner corner it is the straight line through the corner.
  {
    const auto rect = [](float x0, float y0, float x1, float y1, int below, int above) {
      const Vec3 v[4] = {{x0, y0, 0}, {x1, y0, 0}, {x1, y1, 0}, {x0, y1, 0}};
      const int across[4] = {below, -1, above, -1};
      Area a;
      for (int k = 0; k < 4; ++k) {
        a.polygon.push_back(v[k]);
        a.edges.push_back({across[k], v[k], v[(k + 1) % 4]});
      }
      a.center = {(x0 + x1) / 2, (y0 + y1) / 2, 0};
      return a;
    };
    const NavMesh wide({rect(0, 0, 200, 100, -1, 1), rect(0, 100, 200, 200, 0, -1)});
    const Vec3 a{10, 95, 0}, b{10, 105, 0};
    CHECK(std::fabs(wide.ShortestWalk(0, a, 1, b, 20.0f) - 10.0f) < 0.01f);
    CHECK(std::isinf(wide.WalkLength(0, a, 1, b, 20.0f)));
    CHECK(std::fabs(wide.WalkLength(0, a, 1, b, 1000.0f) - 2.0f * std::hypot(90.0f, 5.0f)) < 0.01f);
    CHECK(std::isinf(wide.ShortestWalk(0, a, 1, b, 9.0f)));  // longer than the bound
    // One area: the straight line; out of range or not finite: none.
    CHECK(std::fabs(wide.ShortestWalk(0, {10, 10, 0}, 0, {40, 50, 0}, 100.0f) - 50.0f) < 0.01f);
    CHECK(std::isinf(wide.ShortestWalk(0, a, 2, b, 100.0f)));
    CHECK(std::isinf(wide.ShortestWalk(-1, a, 1, b, 100.0f)));
    CHECK(std::isinf(wide.ShortestWalk(0, {NAN, 95, 0}, 1, b, 100.0f)));

    const NavMesh ell(Squares({{0, 0}, {1, 0}, {1, 1}}));
    const float corner = std::hypot(S, S);  // (50, 50) to (150, 150), through the corner (S, S)
    CHECK(std::fabs(ell.ShortestWalk(0, Center(0, 0), 2, Center(1, 1), 1000.0f) - corner) < 0.01f);
    CHECK(ell.WalkLength(0, Center(0, 0), 2, Center(1, 1), 1000.0f) > corner + 20.0f);
    CHECK(std::isinf(ell.ShortestWalk(0, Center(0, 0), 2, Center(1, 1), corner - 1.0f)));
    // A U: the two ends are 2S apart in a straight line, S + S * sqrt(2) on foot (round the two
    // inner corners of the bottom).
    const NavMesh u(Squares({{0, 1}, {0, 0}, {1, 0}, {2, 0}, {2, 1}}));
    const float round = u.ShortestWalk(0, Center(0, 1), 4, Center(2, 1), 1000.0f);
    CHECK(std::fabs(round - (S + std::hypot(S, S))) < 0.01f);
    CHECK(std::isinf(u.ShortestWalk(0, Center(0, 1), 4, Center(2, 1), 2.0f * S)));
    // Not joined on foot: none, links or not.
    NavMesh apart(Join(Squares(Row(0, 2)), Squares(Row(5, 7))));
    apart.AddLinks(GenerateGapLinks(apart, limits));
    CHECK(std::isinf(apart.ShortestWalk(2, Center(2, 0), 3, Center(5, 0), 1e6f)));
  }

  // Clearance: the corner of an L corridor moves off the inner vertex (2S, S), along the portals,
  // and the path stays on the mesh; a clearance wider than half a portal goes through its middle.
  {
    const NavMesh mesh(Squares({{0, 0}, {1, 0}, {2, 0}, {2, 1}, {2, 2}}));
    const Vec3 vertex{2 * S, S, 0};
    const Plan tight = PlanPath(mesh, Center(0, 0), Center(2, 2));
    CHECK(tight.reached && Parallel(tight));
    bool on_vertex = false;
    for (const Vec3& w : tight.waypoints) on_vertex |= Near(w, vertex);
    CHECK(on_vertex);
    const Plan clear = PlanPath(mesh, Center(0, 0), Center(2, 2), 20.0f);
    CHECK(clear.reached && Parallel(clear));
    REQUIRE_OR_RETURN(clear.waypoints.size() == 3);
    CHECK(Near(clear.waypoints[0], 2 * S, S - 20.0f));
    CHECK(Near(clear.waypoints[1], 2 * S + 20.0f, S));
    CHECK(Near(clear.waypoints[2], Center(2, 2)));
    CHECK(Inside(mesh, Center(0, 0), clear.waypoints));
    for (const Vec3& w : clear.waypoints) CHECK(std::hypot(w.x - vertex.x, w.y - vertex.y) >= 20.0f - 0.01f);
    const Plan middle = PlanPath(mesh, Center(0, 0), Center(2, 2), 60.0f);
    CHECK(middle.reached && Parallel(middle));
    REQUIRE_OR_RETURN(middle.waypoints.size() == 3);
    CHECK(Near(middle.waypoints[0], 2 * S, 0.5f * S));
    CHECK(Near(middle.waypoints[1], 2.5f * S, S));
    CHECK(Inside(mesh, Center(0, 0), middle.waypoints));
    // Along a straight corridor the start and the goal stay as given and the path stays on it.
    const NavMesh row(Squares(Row(0, 9)));
    const Plan straight = PlanPath(row, {10, 10, 0}, {990, 90, 0}, 20.0f);
    CHECK(straight.reached);
    REQUIRE_OR_RETURN(!straight.waypoints.empty());
    CHECK(Near(straight.waypoints.back(), 990, 90));
    CHECK(Inside(row, {10, 10, 0}, straight.waypoints));
  }

  // A drop onto the floor under a bridge: a narrow bridge (one square wide, along y) 200 over a
  // wide floor whose edges are nowhere near the bridge's. Walking off its side lands on the floor.
  {
    std::vector<Cell> floor_cells = Grid(5, 3);
    std::vector<Cell> bridge_cells;
    for (int j = 0; j < 3; ++j) bridge_cells.push_back({2, j});
    std::vector<Area> areas = Squares(floor_cells);
    const int bridge_first = int(areas.size());
    NavMesh mesh(Join(areas, Squares(bridge_cells, 200.0f)));
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    bool onto_floor = false;
    for (const Link& l : links) {
      if (l.from_area < bridge_first || l.to_area >= bridge_first) continue;
      onto_floor |= l.kind == LinkKind::kDrop && std::fabs(l.landing.z) < 0.01f &&
                    std::fabs(l.takeoff.z - 200.0f) < 0.01f;
    }
    CHECK(onto_floor);
    mesh.AddLinks(links);
    const Plan plan = PlanPath(mesh, Center(2, 1, 200.0f), Center(0, 1));
    CHECK(plan.reached && Parallel(plan));
    CHECK(std::find(plan.actions.begin(), plan.actions.end(), Action::kDrop) != plan.actions.end());
  }

  // A shortcut in one island: a U whose two ends face each other across a gap of 2S. The jump is
  // generated because walking round is far longer; disabled, the plan walks the long way.
  {
    std::vector<Cell> cells = {{0, 0}, {1, 0}, {2, 0}, {5, 0}, {6, 0}, {7, 0}};
    for (int j = 1; j <= 4; ++j) cells.push_back({0, j});
    for (int j = 1; j <= 4; ++j) cells.push_back({7, j});
    for (int i = 0; i <= 7; ++i) cells.push_back({i, 5});
    NavMesh mesh(Squares(cells));
    CHECK(mesh.IslandCount() == 1);
    const std::vector<Link> links = GenerateGapLinks(mesh, limits);
    const Link* jump = Find(links, 2, 3);  // cell (2,0) is area 2, cell (5,0) area 3
    REQUIRE_OR_RETURN(jump);
    CHECK(jump->kind == LinkKind::kJump);
    // No hop between areas that walking joins closely: nothing between neighbouring cells.
    for (const Link& l : links) {
      for (const Edge& e : Squares(cells)[l.from_area].edges) CHECK(e.neighbor != l.to_area);
    }
    const int id = jump->id;
    mesh.AddLinks(links);
    const Plan plan = PlanPath(mesh, Center(0, 0), Center(7, 0));
    CHECK(plan.reached && Parallel(plan) && Uses(plan, id) >= 0);
    CHECK(Length(Center(0, 0), plan.waypoints) < 8 * S);
    CHECK(mesh.DisableLink(id));
    const Plan around = PlanPath(mesh, Center(0, 0), Center(7, 0));
    CHECK(around.reached && Parallel(around) && Uses(around, id) == -1);
    CHECK(Length(Center(0, 0), around.waypoints) > 11 * S);
  }

  // A game link the caller adds: a bounce pad up a wall of 400 (above up). Without it the top is
  // unreachable (the drop down joins the island but leads only down); with it the plan walks to
  // the pad and takes the link.
  {
    NavMesh mesh(Join(Squares(Row(0, 2)), Squares(Row(3, 5), 400.0f)));
    const std::vector<Link> generated = GenerateGapLinks(mesh, limits);
    CHECK(!Find(generated, 2, 3));
    const Link* down = Find(generated, 3, 2);
    REQUIRE_OR_RETURN(down);
    CHECK(down->kind == LinkKind::kDrop);
    mesh.AddLinks(generated);
    const Vec3 top = Center(5, 0, 400.0f);
    CHECK(!PlanPath(mesh, Center(0, 0), top).reached);

    const Link pad{2, 3, {2.5f * S, 0.5f * S, 0.0f}, {3.5f * S, 0.5f * S, 400.0f}, LinkKind::kLink,
                   150.0f, 100};
    mesh.AddLinks({pad});
    CHECK(mesh.Links().back().id == 100);
    const Plan plan = PlanPath(mesh, Center(0, 0), top);
    CHECK(plan.reached && Parallel(plan));
    REQUIRE_OR_RETURN(plan.waypoints.size() == 3);
    CHECK(Near(plan.waypoints[0], pad.takeoff) && plan.actions[0] == Action::kWalk);
    CHECK(Near(plan.waypoints[1], pad.landing) && plan.actions[1] == Action::kLink);
    CHECK(plan.links[1] == 100);
    CHECK(Near(plan.waypoints[2], top) && plan.actions[2] == Action::kWalk);

    // Ids: a taken or negative id gets the next free one; a bad link is left out; a missing cost
    // becomes the length; generated ids start above every id so far.
    const size_t before = mesh.Links().size();
    Link taken = pad;
    taken.id = 100;
    Link negative = pad;
    negative.id = -1;
    negative.cost = -1.0f;
    Link bad = pad;
    bad.to_area = 99;
    Link self = pad;
    self.to_area = self.from_area;
    mesh.AddLinks({taken, negative, bad, self});
    REQUIRE_OR_RETURN(mesh.Links().size() == before + 2);
    CHECK(mesh.Links()[before].id == 101);
    CHECK(mesh.Links()[before + 1].id == 102);
    CHECK(std::fabs(mesh.Links()[before + 1].cost - std::hypot(S, 400.0f)) < 0.01f);
    for (const Link& l : GenerateGapLinks(mesh, limits)) CHECK(l.id > 102);

    // A teleporter cheaper than its length: the plan still takes the cheapest way.
    NavMesh far(Join(Squares(Row(0, 2)), Squares(Row(50, 52))));
    far.AddLinks({{0, 3, Center(0, 0), Center(50, 0), LinkKind::kLink, 10.0f, -1}});
    const Plan teleport = PlanPath(far, Center(2, 0), Center(52, 0));
    CHECK(teleport.reached && Parallel(teleport));
    REQUIRE_OR_RETURN(teleport.waypoints.size() == 3);
    CHECK(teleport.actions[1] == Action::kLink && Near(teleport.waypoints[0], Center(0, 0)));
  }

  return Finish("nav planner");
}
