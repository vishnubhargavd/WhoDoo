'use client'

import { Canvas, useFrame, useThree } from '@react-three/fiber'
import Image from 'next/image'
import { useMemo, useRef, useState } from 'react'
import type { MotionValue } from 'motion/react'
import * as THREE from 'three'
import { AnimatedRibbon, RibbedSphere } from './animated-ribbon'
import { RIBBONS, SPHERES } from './ribbon-paths'

type HeroSceneProps = {
  animate: boolean
  active: boolean
  pointerX: MotionValue<number>
  pointerY: MotionValue<number>
}

function supportsWebGL() {
  try {
    const canvas = document.createElement('canvas')
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'))
  } catch {
    return false
  }
}

function Sculpture({ animate }: { animate: boolean }) {
  const time = useMemo(() => ({ value: 0 }), [])
  const group = useRef<THREE.Group>(null)

  useFrame((_, delta) => {
    if (!animate) return
    time.value += Math.min(delta, 0.1)
    if (group.current) {
      group.current.rotation.z = Math.sin(time.value * 0.08) * 0.025
      group.current.position.y = Math.sin(time.value * 0.12) * 0.08
    }
  })

  return (
    <group ref={group}>
      {RIBBONS.map((spec) => (
        <AnimatedRibbon key={spec.id} spec={spec} time={time} />
      ))}
      {SPHERES.map((spec) => (
        <RibbedSphere key={spec.id} spec={spec} time={time} />
      ))}
    </group>
  )
}

function CameraRig({ animate, pointerX, pointerY }: Omit<HeroSceneProps, 'active'>) {
  useFrame(({ camera }, delta) => {
    if (!animate) return
    camera.position.x = THREE.MathUtils.damp(camera.position.x, pointerX.get() * 0.4, 2.2, delta)
    camera.position.y = THREE.MathUtils.damp(camera.position.y, -pointerY.get() * 0.28, 2.2, delta)
    camera.lookAt(0, 0, 0)
  })
  return null
}

function ShadowCatcher() {
  const { width, height } = useThree((state) => state.viewport)
  return (
    <mesh position={[0, 0, -2.8]} receiveShadow>
      <planeGeometry args={[width * 2, height * 2]} />
      <shadowMaterial transparent opacity={0.16} color="#6b2112" />
    </mesh>
  )
}

export function HeroScene({ animate, active, pointerX, pointerY }: HeroSceneProps) {
  const [webgl] = useState(supportsWebGL)
  const [ready, setReady] = useState(false)

  if (!webgl) {
    return <Image src="/images/hero-bg.webp" alt="" fill priority sizes="100vw" className="object-cover" />
  }

  return (
    <div className="absolute inset-0 transition-opacity duration-1000 ease-out" style={{ opacity: ready ? 1 : 0 }}>
      <Canvas
        dpr={[1, 1.75]}
        shadows={{ type: THREE.PCFShadowMap }}
        frameloop={animate && active ? 'always' : 'demand'}
        camera={{ position: [0, 0, 12], fov: 32 }}
        gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
        onCreated={() => setReady(true)}
      >
        <ambientLight intensity={0.5} color="#fff1e6" />
        <hemisphereLight args={['#fff6ea', '#d9503b', 0.75]} />
        <directionalLight
          position={[-5, 7, 9]}
          intensity={2.6}
          color="#fff4ea"
          castShadow
          shadow-mapSize={[2048, 2048]}
          shadow-radius={10}
          shadow-bias={-0.0004}
          shadow-camera-left={-11}
          shadow-camera-right={11}
          shadow-camera-top={8}
          shadow-camera-bottom={-8}
          shadow-camera-near={1}
          shadow-camera-far={40}
        />
        <directionalLight position={[7, -3, 5]} intensity={0.7} color="#ffd2c2" />
        <Sculpture animate={animate} />
        <ShadowCatcher />
        <CameraRig animate={animate} pointerX={pointerX} pointerY={pointerY} />
      </Canvas>
    </div>
  )
}
