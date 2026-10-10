// x / y are normalised to half the visible viewport (-1 = left/bottom edge, 1 = right/top edge).
// z is in world units. radius is a fraction of the scene's "unit" size (see AnimatedRibbon).
export type Vec3 = [number, number, number]

export type RibbonSpec = {
  id: string
  points: Vec3[]
  radius: number
  phase: number
  amp: number
  capEnd?: boolean
}

export type SphereSpec = {
  id: string
  center: Vec3
  radius: number
  tilt: Vec3
  phase: number
  amp: number
}

export const RIBBONS: RibbonSpec[] = [
  {
    id: 'main-s',
    points: [
      [-0.6, 1.4, -1.2],
      [-0.48, 0.75, -0.8],
      [-0.78, 0.08, -0.3],
      [-0.84, -0.46, 0],
      [-0.5, -0.7, 0.25],
      [0.05, -0.46, 0],
      [0.55, -0.16, -0.35],
      [0.94, -0.14, -0.2],
      [1.04, -0.58, 0.05],
      [0.78, -1.45, 0.25],
    ],
    radius: 0.092,
    phase: 0,
    amp: 0.16,
  },
  {
    id: 'top-drop',
    points: [
      [0.02, 1.5, -2],
      [0.12, 1.1, -1.8],
      [0.32, 0.86, -1.7],
    ],
    radius: 0.11,
    phase: 2.1,
    amp: 0.12,
    capEnd: true,
  },
  {
    id: 'bottom-sweep',
    points: [
      [-1.45, -1.02, 0.4],
      [-0.75, -0.86, 0.5],
      [-0.15, -0.98, 0.4],
      [0.3, -1.45, 0.3],
    ],
    radius: 0.075,
    phase: 4.2,
    amp: 0.14,
  },
]

export const SPHERES: SphereSpec[] = [
  {
    id: 'top-right-orb',
    center: [0.66, 0.64, -1.6],
    radius: 0.2,
    tilt: [0.5, 0.2, -0.75],
    phase: 1.3,
    amp: 0.1,
  },
]
