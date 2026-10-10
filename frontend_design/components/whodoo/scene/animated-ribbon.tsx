'use client'

import { useThree } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import * as THREE from 'three'
import type { RibbonSpec, SphereSpec } from './ribbon-paths'

export type TimeUniform = { value: number }

// Low-frequency, periodic displacement in world space, so tube + cap stay welded and loops never jump.
const SWAY_GLSL = /* glsl */ `
  float swayA = sin(position.y * 0.32 + uTime * 0.42 + uPhase);
  float swayB = cos(position.x * 0.27 + uTime * 0.33 + uPhase * 1.7);
  float swayC = sin((position.x + position.y) * 0.2 + uTime * 0.25 + uPhase * 0.6);
  transformed += vec3(swayA, swayB * 0.8, swayC * 1.4) * uAmp;
`

type GrooveAxis = 'u' | 'v'

function createGrooveTexture(axis: GrooveAxis, repeat: number) {
  const period = 64
  const width = axis === 'u' ? period : 4
  const height = axis === 'u' ? 4 : period
  const data = new Uint8Array(width * height * 4)

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const t = (axis === 'u' ? x : y) / period
      const slope = Math.sin(t * Math.PI * 2) + 0.35 * Math.sin(t * Math.PI * 4)
      const strength = 0.6
      const nx = axis === 'u' ? slope * strength : 0
      const ny = axis === 'v' ? slope * strength : 0
      const len = Math.hypot(nx, ny, 1)
      const i = (y * width + x) * 4
      data[i] = ((nx / len) * 0.5 + 0.5) * 255
      data[i + 1] = ((ny / len) * 0.5 + 0.5) * 255
      data[i + 2] = ((1 / len) * 0.5 + 0.5) * 255
      data[i + 3] = 255
    }
  }

  const texture = new THREE.DataTexture(data, width, height, THREE.RGBAFormat)
  texture.wrapS = THREE.RepeatWrapping
  texture.wrapT = THREE.RepeatWrapping
  texture.repeat.set(axis === 'u' ? repeat : 1, axis === 'u' ? 1 : repeat)
  texture.magFilter = THREE.LinearFilter
  texture.minFilter = THREE.LinearMipmapLinearFilter
  texture.generateMipmaps = true
  texture.anisotropy = 4
  texture.needsUpdate = true
  return texture
}

function useClayMaterials(time: TimeUniform, phase: number, amp: number, axis: GrooveAxis, repeat: number) {
  const materials = useMemo(() => {
    const normalMap = createGrooveTexture(axis, repeat)
    const uniforms = { uTime: time, uPhase: { value: phase }, uAmp: { value: amp } }

    const patch = (shader: THREE.WebGLProgramParametersWithUniforms) => {
      Object.assign(shader.uniforms, uniforms)
      shader.vertexShader =
        'uniform float uTime;\nuniform float uPhase;\nuniform float uAmp;\n' +
        shader.vertexShader.replace('#include <begin_vertex>', `#include <begin_vertex>\n${SWAY_GLSL}`)
    }

    const material = new THREE.MeshPhysicalMaterial({
      color: '#F04B37',
      roughness: 0.6,
      metalness: 0,
      sheen: 0.7,
      sheenRoughness: 0.45,
      sheenColor: new THREE.Color('#ff9f84'),
      normalMap,
      normalScale: new THREE.Vector2(0.9, 0.9),
    })
    material.onBeforeCompile = patch

    const depthMaterial = new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking })
    depthMaterial.onBeforeCompile = patch

    return { material, depthMaterial, normalMap }
  }, [time, phase, amp, axis, repeat])

  useEffect(
    () => () => {
      materials.material.dispose()
      materials.depthMaterial.dispose()
      materials.normalMap.dispose()
    },
    [materials],
  )

  return materials
}

function useSceneUnit() {
  const { width, height } = useThree((state) => state.viewport)
  return { width, height, unit: Math.min(width * 0.72, height) }
}

export function AnimatedRibbon({ spec, time }: { spec: RibbonSpec; time: TimeUniform }) {
  const { width, height, unit } = useSceneUnit()
  const tube = useClayMaterials(time, spec.phase, spec.amp, 'v', 40)
  const cap = useClayMaterials(time, spec.phase, spec.amp, 'u', 40)

  const { tubeGeometry, capGeometry } = useMemo(() => {
    const radius = spec.radius * unit
    const curve = new THREE.CatmullRomCurve3(
      spec.points.map(([x, y, z]) => new THREE.Vector3((x * width) / 2, (y * height) / 2, z)),
      false,
      'centripetal',
    )
    const segments = Math.max(160, Math.round(curve.getLength() * 28))
    const tubeGeometry = new THREE.TubeGeometry(curve, segments, radius, 64, false)

    let capGeometry: THREE.SphereGeometry | null = null
    if (spec.capEnd) {
      const end = curve.getPoint(1)
      const tangent = curve.getTangent(1)
      capGeometry = new THREE.SphereGeometry(radius, 64, 48)
      capGeometry.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), tangent))
      capGeometry.translate(end.x, end.y, end.z)
    }
    return { tubeGeometry, capGeometry }
  }, [spec, width, height, unit])

  useEffect(
    () => () => {
      tubeGeometry.dispose()
      capGeometry?.dispose()
    },
    [tubeGeometry, capGeometry],
  )

  return (
    <>
      <mesh geometry={tubeGeometry} material={tube.material} customDepthMaterial={tube.depthMaterial} castShadow receiveShadow />
      {capGeometry && (
        <mesh geometry={capGeometry} material={cap.material} customDepthMaterial={cap.depthMaterial} castShadow receiveShadow />
      )}
    </>
  )
}

export function RibbedSphere({ spec, time }: { spec: SphereSpec; time: TimeUniform }) {
  const { width, height, unit } = useSceneUnit()
  const { material, depthMaterial } = useClayMaterials(time, spec.phase, spec.amp, 'u', 46)

  const geometry = useMemo(() => {
    const [cx, cy, cz] = spec.center
    const geo = new THREE.SphereGeometry(spec.radius * unit, 96, 64)
    geo.applyQuaternion(new THREE.Quaternion().setFromEuler(new THREE.Euler(...spec.tilt)))
    geo.translate((cx * width) / 2, (cy * height) / 2, cz)
    return geo
  }, [spec, width, height, unit])

  useEffect(() => () => geometry.dispose(), [geometry])

  return <mesh geometry={geometry} material={material} customDepthMaterial={depthMaterial} castShadow receiveShadow />
}
