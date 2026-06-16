// Thin REST client for the existing dashboard API (unchanged backend).
const API = '/api/v1/dashboard/api';

let TOKEN = localStorage.getItem('ja_token') || '';
let ROLE = localStorage.getItem('ja_role') || '';
let NAME = localStorage.getItem('ja_name') || '';

export function getAuth() {
  return { token: TOKEN, role: ROLE, name: NAME };
}
export function setAuth(token: string, role: string, name: string) {
  TOKEN = token; ROLE = role; NAME = name;
  localStorage.setItem('ja_token', token);
  localStorage.setItem('ja_role', role);
  localStorage.setItem('ja_name', name);
}
export function logout() {
  localStorage.clear();
  TOKEN = '';
  location.reload();
}
export const canAct = () => ROLE === 'admin' || ROLE === 'approver';
export const isAdmin = () => ROLE === 'admin';

export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'ngrok-skip-browser-warning': '1',
    Authorization: 'Bearer ' + TOKEN,
    ...((opts.headers as Record<string, string>) || {}),
  };
  const r = await fetch(API + path, { ...opts, headers });
  if (r.status === 401) {
    logout();
    throw new Error('Session expired');
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error((d as any).detail || 'HTTP ' + r.status);
  }
  return r.json();
}

export interface LoginResp {
  access_token: string;
  role: string;
  full_name: string;
  email: string;
}
export async function login(email: string, password: string): Promise<LoginResp> {
  const r = await fetch(API + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': '1' },
    body: JSON.stringify({ email, password }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || 'Login failed');
  return d as LoginResp;
}
