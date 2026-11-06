#pragma once

// A path planner over a navigation mesh of convex areas: islands, point location, A* over the
// areas and a funnel over the shared edges, plus links (jumps, drops and the game's own links such
// as bounce pads) that join areas the mesh leaves apart. Game-independent and without the SDK: the
// game fills a NavMesh from whatever it reads (Swap Force Online's developer `navto` fills it from
// the level's navmesh in guest memory), adds links, and gets back waypoints with an action each
// for its own walker. Everything here is in memory; nothing is read from or written to disk.

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace skylanders::nav {

// A point in world units, z up.
struct Vec3 {
  float x, y, z;
};

// One side of an area: the edge from vertex a to vertex b (counter-clockwise in xy, so the area's
// inside is on the left of a->b). neighbor is the area across the edge, -1 for a boundary.
struct Edge {
  int neighbor;
  Vec3 a, b;
};

// A convex area (convex in xy). polygon holds its vertices counter-clockwise in xy; edges its
// sides in the same order; center a point inside it.
struct Area {
  std::vector<Vec3> polygon;
  std::vector<Edge> edges;
  Vec3 center;
};

// What a link is. kJump: jump at the takeoff and steer at the landing (across a gap or up a
// ledge). kDrop: walk off at the takeoff and fall to the landing, no button. kLink: a link of the
// game's own (bounce pad, teleporter, nav link) that the caller adds and knows how to use.
enum class LinkKind : uint8_t { kJump, kDrop, kLink };

// A one-way link from from_area to to_area: the walker goes to takeoff (a point of from_area),
// does what kind says, and arrives at landing (a point of to_area). cost is what A* counts for the
// link itself, on top of the walk to the takeoff and on from the landing. id names the link for
// DisableLink and Plan::links.
struct Link {
  int from_area;
  int to_area;
  Vec3 takeoff;
  Vec3 landing;
  LinkKind kind;
  float cost;
  int id;
};

// What the walker can jump, in world units: reach the horizontal gap, up the height it can jump
// onto, down the height it can fall safely, min_gap the distance below which two edges touch (no
// link). The defaults are guesses; dev_jumptest measures the real values.
struct JumpLimits {
  float reach = 300;
  float up = 100;
  float down = 600;
  float min_gap = 20;
};

struct Plan;

class NavMesh {
 public:
  // Takes the areas and works out the islands and what the point lookup needs. No links yet.
  explicit NavMesh(std::vector<Area> areas);

  size_t AreaCount() const { return areas_.size(); }
  // The island (connected group of areas, by neighbor links and enabled Links, each taken in either
  // direction) of an area, -1 for an index out of range. Islands are numbered 0..IslandCount()-1 in
  // order of their first area.
  int Island(int area) const;
  size_t IslandCount() const { return island_count_; }
  // The area whose xy polygon holds p (edges included) and whose floor at p's xy is nearest to p.z,
  // no more than max_dz away; -1 when there is none.
  int FindArea(const Vec3& p, float max_dz = 200.0f) const;
  // True when every neighbor link has its reverse (area i names j across an edge, and j names i
  // across one of its own). A link to an index out of range has no reverse.
  bool Symmetric() const;

  // Adds one-way links to the graph and works out the islands again. A link whose areas are out of
  // range or the same area, or whose points are not finite, is left out. A link keeps its id when
  // the id is not negative and not taken (by an earlier link or one before it in this call);
  // otherwise it gets the next free id (one above the highest so far). A cost that is negative or
  // not a number becomes the straight distance from takeoff to landing. Links() has the result.
  void AddLinks(std::vector<Link> links);
  // Every link added so far, disabled ones included, in the order they were added.
  const std::vector<Link>& Links() const { return links_; }
  // Takes a link out of planning (it failed in practice) and works out the islands again. Returns
  // false when no link has that id. A disabled link stays in Links().
  bool DisableLink(int id);
  // True when a link with that id was disabled; false for an enabled or unknown id.
  bool LinkDisabled(int id) const;

 private:
  friend Plan PlanPath(const NavMesh& mesh, const Vec3& start, const Vec3& goal);
  friend std::vector<Link> GenerateGapLinks(const NavMesh& mesh, const JumpLimits& limits);

  // Per-area facts worked out once in the constructor.
  struct Shape {
    float min_x, min_y, max_x, max_y;  // xy bounding box of the polygon
    Vec3 normal;                       // plane normal (Newell), z >= 0
    Vec3 origin;                       // the polygon's vertex average, a point on that plane
    bool ccw;                          // false when the polygon is clockwise in xy after all
  };

  bool ValidNeighbor(int area, int neighbor) const {
    return neighbor >= 0 && size_t(neighbor) < areas_.size() && neighbor != area;
  }
  bool ContainsXY(int area, float x, float y) const;
  // The floor height of the area's plane at (x, y); the vertex average's z for a vertical polygon.
  float FloorZ(int area, float x, float y) const;
  // The point of the area (in xy, z on its floor) closest to (x, y).
  Vec3 ClosestPointXY(int area, float x, float y) const;
  // Flood fill over the neighbor links and the enabled links; also rebuilds out_links_.
  void ComputeIslands();

  std::vector<Area> areas_;
  std::vector<Shape> shapes_;
  std::vector<int> island_;
  size_t island_count_ = 0;
  std::vector<Link> links_;
  std::vector<char> link_disabled_;          // parallel to links_
  std::vector<std::vector<int>> out_links_;  // per area: indices into links_ of its enabled links
  int next_link_id_ = 0;
};

// What the walker does to reach a waypoint: walk to it, or arrive at it by the link's action.
enum class Action : uint8_t { kWalk, kJump, kDrop, kLink };

// The result of a plan. waypoints run from the start (left out) to the goal (included); a walker
// steers at each in turn. actions and links are parallel to waypoints: for a link the plan has its
// takeoff (kWalk, link -1) and then its landing (the link's action, the link's id); every other
// waypoint is kWalk with link -1. When reached is false, why says what went wrong and the waypoints
// (maybe none) lead to the closest point of the goal's xy that the start can reach.
struct Plan {
  bool reached = false;
  std::vector<Vec3> waypoints;
  std::vector<Action> actions;
  std::vector<int> links;
  std::string why;
};

// Plans from start to goal: both are located with FindArea, then A* runs over the areas (the cost
// of a step is the distance between the points where the path crosses into an area, the midpoints
// of the shared edges; an enabled link costs the walk to its takeoff plus its cost, and the path
// enters to_area at the landing), and a funnel (string pulling) over the shared edges of each
// walking stretch between links turns it into the shortest line through them in xy, with a
// waypoint at each corner. When the start is off the mesh there is no plan. When the goal is off
// the mesh, on another island, or not reachable over one-way neighbor links and links, the plan
// goes to the closest point of the goal's xy among the areas the start can reach, with reached
// false. A link cheaper than its straight length (a teleporter) scales the A* heuristic down so it
// stays exact.
Plan PlanPath(const NavMesh& mesh, const Vec3& start, const Vec3& goal);

// Candidate jumps and drops for the mesh, pure geometry: the caller checks them in the world (a
// wall between the two edges, a low ceiling) before AddLinks, for example with raycasts. Ids start
// one above the highest id in mesh.Links(). Two rules:
// - Edge pairs. For two boundary edges (no neighbor) of different areas, the closest points in xy
//   (the middle of the overlap for parallel edges) give the takeoff on the first and the landing
//   point on the second. The pair is a link when the xy gap is at most reach, the landing is at
//   most up above and at most down below the takeoff, the two points are at least min_gap apart
//   (closer edges touch), and the edges face each other: the jump leaves the first area across its
//   edge and enters the second across its edge (for edges stacked in xy, the two edges' outward
//   normals point against each other). The landing is pushed min_gap inside the second area. A
//   link that goes down by at least min_gap with an xy gap of at most reach / 3 is a kDrop (walk
//   off), every other one a kJump.
// - Floors below. From three points of every boundary edge (a quarter, half and three quarters
//   along), 2 * min_gap straight out, the first floor below (the highest area under that point
//   lower than the edge by at least min_gap, with no floor within min_gap of the edge's height
//   there) is a kDrop when it is at most down below and the point is at least min_gap / 2 inside
//   that area's boundary edges. This finds the floor under a bridge, which has no edge beside the
//   bridge's edge.
// A candidate between areas that walking connects is kept only when the walk from takeoff to
// landing (searched up to that length) is longer than twice the link's length plus min_gap: a
// shortcut across a pit, not a hop along a floor. Of all candidates from one area to another the
// cheapest is kept. The cost of a generated link is twice its length (a jump can fail). Edges are
// only compared with edges within reach of them, through a uniform grid over the boundary edges.
std::vector<Link> GenerateGapLinks(const NavMesh& mesh, const JumpLimits& limits);

}  // namespace skylanders::nav
