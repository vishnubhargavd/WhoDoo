'use client';

import { useEffect, useRef, useState } from 'react';
import type { BrowserQRCodeReader, IScannerControls } from '@zxing/browser';
import { cameraFailure, createDuplicateScanGuard, createScannerLifecycle, parseScannedCredential } from '../scanner-state.ts';
import type { ScanFeedback } from '../scanner-state.ts';

type CameraState = 'ready' | 'starting' | 'scanning' | 'denied' | 'unavailable' | 'initialization';
const cameraMessages: Record<CameraState, string> = {
  ready: 'Camera is off. Start scanning when you are ready.',
  starting: 'Opening camera…',
  scanning: 'Camera is on. Hold a ticket QR code inside the frame.',
  denied: 'Camera permission was denied. Allow camera access in your browser settings, then try again.',
  unavailable: 'No usable camera was found on this device.',
  initialization: 'Camera could not start. Check that the page uses HTTPS and try again.'
};

function mediaStream(video: HTMLVideoElement | null): MediaStream | null {
  return video?.srcObject instanceof MediaStream ? video.srcObject : null;
}

export function MobileScanner() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const lifecycle = useRef(createScannerLifecycle());
  const scanGuard = useRef(createDuplicateScanGuard());
  const startInProgress = useRef(false);
  const captured = useRef(false);
  const mounted = useRef(false);
  const feedbackRef = useRef<HTMLParagraphElement>(null);
  const [camera, setCamera] = useState<CameraState>('ready');
  const [feedback, setFeedback] = useState<ScanFeedback>({ state: 'ready', message: 'Start the camera to scan a ticket.' });
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [selectedDevice, setSelectedDevice] = useState('');

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      lifecycle.current.stop();
      const video = videoRef.current;
      mediaStream(video)?.getTracks().forEach(track => track.stop());
      if (video) video.srcObject = null;
    };
  }, []);

  useEffect(() => {
    if (feedback.state !== 'ready' && feedback.state !== 'scanning') feedbackRef.current?.focus();
  }, [feedback]);

  function stopCamera() {
    lifecycle.current.stop();
    const video = videoRef.current;
    mediaStream(video)?.getTracks().forEach(track => track.stop());
    if (video) video.srcObject = null;
    if (mounted.current) setCamera('ready');
  }

  async function startCamera(deviceId?: string) {
    if (startInProgress.current || !videoRef.current) return;
    if (!window.isSecureContext && !['localhost', '127.0.0.1'].includes(window.location.hostname)) {
      setCamera('initialization');
      setFeedback({ state: 'network_unavailable', message: 'Camera access requires a secure HTTPS connection.' });
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setCamera('unavailable');
      setFeedback({ state: 'network_unavailable', message: 'This browser does not provide camera access.' });
      return;
    }
    startInProgress.current = true;
    captured.current = false;
    scanGuard.current.reset();
    setCamera('starting');
    setFeedback({ state: 'ready', message: 'Opening the camera…' });
    try {
      const { BrowserQRCodeReader, BrowserCodeReader } = await import('@zxing/browser');
      const decoder: BrowserQRCodeReader = new BrowserQRCodeReader();
      const video = videoRef.current;
      if (!video) return;
      const controls = await decoder.decodeFromVideoDevice(deviceId || undefined, video,
        (result, _error, callbackControls: IScannerControls) => {
          if (!result || captured.current || !scanGuard.current.accept(Date.now())) return;
          captured.current = true;
          lifecycle.current.attach(callbackControls, mediaStream(video));
          lifecycle.current.stop();
          video.srcObject = null;
          if (!mounted.current) return;
          setCamera('ready');
          // The raw bearer value is used only for this local format check, then discarded.
          setFeedback(parseScannedCredential(result.getText()));
        });
      if (!mounted.current || captured.current) { controls.stop(); return; }
      lifecycle.current.attach(controls, mediaStream(video));
      try {
        const available = (await BrowserCodeReader.listVideoInputDevices()).filter(device => device.deviceId);
        if (mounted.current) {
          setDevices(available);
          if (!deviceId && available.length) {
            const preferred = available.find(device => /back|rear|environment/i.test(device.label)) ?? available[0];
            if (preferred) setSelectedDevice(preferred.deviceId);
          }
        }
      } catch { if (mounted.current) setDevices([]); }
      if (mounted.current) {
        setCamera('scanning');
        setFeedback({ state: 'scanning', message: 'Camera is on. Hold a ticket QR code inside the frame.' });
      }
    } catch (error) {
      lifecycle.current.stop();
      const failure = cameraFailure(error);
      if (mounted.current) {
        setCamera(failure);
        setFeedback({ state: failure === 'denied' ? 'authorization_denied' : 'network_unavailable', message: cameraMessages[failure] });
      }
    } finally { startInProgress.current = false; }
  }

  const canSwitch = devices.length > 1 && camera === 'scanning';
  return <section aria-labelledby="scanner-title" className="space-y-5 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-7">
    <div><p className="text-sm font-semibold uppercase tracking-wide text-teal-700">GateSync · mobile scanner</p>
      <h2 id="scanner-title" className="mt-1 text-xl font-semibold">Scan a ticket QR</h2>
      <p className="mt-2 text-sm leading-6 text-slate-600">Camera access starts only after you press the button. Keep the QR inside the frame until it is captured.</p></div>
    <div className="relative mx-auto aspect-[4/3] w-full max-w-xl overflow-hidden rounded-2xl bg-slate-950">
      <video ref={videoRef} muted playsInline aria-label="Live camera preview for scanning WhoDoo ticket QR codes" className="h-full w-full object-cover" />
      <div aria-hidden="true" className="pointer-events-none absolute inset-[14%] rounded-2xl border-2 border-white/90 shadow-[0_0_0_999px_rgba(2,6,23,0.24)]" />
      <p className="sr-only">Align the full QR code within the visible square guide.</p>
    </div>
    <p className="text-sm text-slate-600" role="status" aria-live="polite">{cameraMessages[camera]}</p>
    {canSwitch && <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <label className="flex-1 text-sm font-medium">Camera
        <select value={selectedDevice} onChange={event => setSelectedDevice(event.target.value)} className="mt-1 block min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3">
          {devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `Camera ${index + 1}`}</option>)}
        </select>
      </label>
      <button type="button" onClick={() => { stopCamera(); void startCamera(selectedDevice); }} className="min-h-11 rounded-lg border border-slate-300 px-4 font-medium hover:bg-slate-50">Switch camera</button>
    </div>}
    <div className="flex flex-wrap gap-3">
      {camera === 'scanning' ? <button type="button" onClick={stopCamera} className="min-h-11 rounded-lg bg-slate-900 px-5 font-semibold text-white hover:bg-slate-700">Stop camera</button>
        : feedback.state === 'credential_captured' || feedback.state === 'invalid_qr' ? <button type="button" onClick={() => { setFeedback({ state: 'ready', message: 'Ready for another scan.' }); void startCamera(selectedDevice || undefined); }} className="min-h-11 rounded-lg bg-teal-700 px-5 font-semibold text-white hover:bg-teal-800">Scan another ticket</button>
          : <button type="button" disabled={camera === 'starting'} onClick={() => void startCamera(selectedDevice || undefined)} className="min-h-11 rounded-lg bg-teal-700 px-5 font-semibold text-white hover:bg-teal-800 disabled:opacity-60">{camera === 'starting' ? 'Starting camera…' : 'Start camera'}</button>}
    </div>
    <p ref={feedbackRef} tabIndex={-1} role={feedback.state === 'invalid_qr' || feedback.state === 'authorization_denied' ? 'alert' : 'status'} aria-live="polite" className="rounded-xl bg-slate-50 p-4 text-sm leading-6 text-slate-700">{feedback.message}</p>
    <p className="text-xs leading-5 text-slate-500">Camera scanning only reads the code. Event staff permissions and the authenticated redemption service are not connected, so no scan in this interface can change attendance.</p>
  </section>;
}
