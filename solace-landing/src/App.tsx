import { useState, useRef } from 'react'
import { motion, useInView } from 'motion/react'
import Footer from './components/Footer'

/* ─── Reveal wrapper ────────────────────────────────────────────────────── */
function Reveal({
  children,
  delay = 0,
  className = '',
  style = {},
}: {
  children?: React.ReactNode
  delay?: number
  className?: string
  style?: React.CSSProperties
}) {
  const ref = useRef<HTMLDivElement>(null)
  const inView = useInView(ref, { once: true, amount: 0.08 })
  return (
    <motion.div
      ref={ref}
      className={className}
      style={style}
      initial={{ opacity: 0, y: 24 }}
      animate={inView ? { opacity: 1, y: 0 } : {}}
      transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1], delay }}
    >
      {children}
    </motion.div>
  )
}

/* ─── Logo SVG paths ────────────────────────────────────────────────────── */
const LOGO_SVG = (
  <>
    <circle cx="60" cy="60" r="54" stroke="white" strokeWidth="2" fill="none" opacity="0.9" />
    <path d="M38 42 C38 34 48 28 60 28 C72 28 82 34 82 42 C82 52 72 56 60 60 C48 64 38 68 38 78 C38 86 48 92 60 92 C72 92 82 86 82 78"
          stroke="white" strokeWidth="3.5" fill="none" strokeLinecap="round" />
  </>
)

const NAV_LINKS = ['Platform', 'Features', 'Pricing', 'About']

/* ─── App ───────────────────────────────────────────────────────────────── */
export default function App() {
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <div className="font-manrope relative min-h-screen bg-[#CC0000] overflow-x-hidden">

      {/* ── Background video ───────────────────────────────────────────── */}
      <div className="absolute inset-0 z-0 overflow-hidden">
        <video
          autoPlay muted loop playsInline
          className="absolute inset-0 w-full h-full object-cover opacity-30"
          src="https://res.cloudinary.com/democloud/video/upload/v1/samples/sea-turtle"
        />
        {/* left gradient overlay */}
        <div
          className="absolute inset-y-0 left-0 w-full"
          style={{
            background: 'linear-gradient(to right, #CC0000 0%, #CC0000 35%, rgba(204,0,0,0.85) 60%, rgba(204,0,0,0.5) 80%, transparent 100%)',
          }}
        />
        {/* bottom gradient */}
        <div
          className="absolute bottom-0 left-0 w-full h-[200px]"
          style={{
            background: 'linear-gradient(to top, #CC0000 0%, rgba(204,0,0,0.85) 25%, rgba(204,0,0,0.5) 60%, transparent 100%)',
          }}
        />
      </div>

      {/* ── Navbar ─────────────────────────────────────────────────────── */}
      <nav className="relative z-30 w-full flex items-center justify-between px-8 md:px-16 py-6 bg-red-700/40 backdrop-blur-md border-b border-white/10">
        {/* Brand */}
        <span className="font-italiana text-white text-2xl tracking-widest select-none">
          Solace
        </span>

        {/* Desktop links */}
        <ul className="hidden md:flex items-center gap-10 list-none">
          {NAV_LINKS.map(l => (
            <li key={l}>
              <a
                href="#"
                className="font-manrope text-white/70 text-sm tracking-wider hover:text-white transition-colors duration-200"
              >
                {l}
              </a>
            </li>
          ))}
        </ul>

        {/* CTA */}
        <a
          href="/login"
          className="hidden md:inline-flex items-center gap-2 bg-white text-red-600 font-semibold text-sm px-5 py-2.5 rounded-full hover:bg-white/90 transition-all duration-200 shadow-sm"
        >
          Request Access
        </a>

        {/* Mobile hamburger */}
        <button
          className="md:hidden text-white p-1"
          onClick={() => setMenuOpen(o => !o)}
          aria-label="Toggle menu"
        >
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            {menuOpen
              ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>
              : <><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></>
            }
          </svg>
        </button>

        {/* Mobile menu */}
        <div
          className="absolute top-full left-0 w-full bg-red-700/95 backdrop-blur-md border-b border-white/10 md:hidden overflow-hidden transition-all duration-300"
          style={{
            maxHeight: menuOpen ? '300px' : '0',
            opacity:   menuOpen ? 1 : 0,
          }}
        >
          <ul className="flex flex-col px-8 py-6 gap-5 list-none">
            {NAV_LINKS.map(l => (
              <li key={l}>
                <a href="#" className="text-white/80 text-sm tracking-wider hover:text-white">
                  {l}
                </a>
              </li>
            ))}
            <li>
              <a
                href="/login"
                className="inline-flex items-center gap-2 bg-white text-red-600 font-semibold text-sm px-5 py-2.5 rounded-full"
              >
                Request Access
              </a>
            </li>
          </ul>
        </div>
      </nav>

      {/* ── Hero body ───────────────────────────────────────────────────── */}
      <div className="relative z-10 flex flex-col items-center w-full pt-20 md:pt-28 pb-24">
        <div className="flex flex-col items-center w-full px-8 text-center max-w-[860px] mx-auto">

          {/* Logo with rings */}
          <Reveal delay={0.05} className="relative flex items-center justify-center mb-12" style={{ width: 160, height: 160 }}>
            {[
              { size: 108, border: '1.5px', opacity: 0.9, delay: '0s'   },
              { size: 130, border: '1px',   opacity: 0.6, delay: '0.4s' },
              { size: 154, border: '0.5px', opacity: 0.3, delay: '0.8s' },
            ].map(({ size, border, opacity, delay: d }) => (
              <span
                key={size}
                className="absolute rounded-full"
                style={{
                  width:     size,
                  height:    size,
                  border:    `${border} solid rgba(255,255,255,${opacity})`,
                  animation: `ring-pulse 2.8s ease-in-out ${d} infinite`,
                }}
              />
            ))}
            <svg className="relative z-10" width="76" height="76" viewBox="0 0 120 120" fill="none">
              {LOGO_SVG}
            </svg>
          </Reveal>

          {/* Brand label */}
          <Reveal delay={0.15} className="font-italiana text-white text-[11px] tracking-[0.7em] uppercase mb-20 opacity-55">
            SOLACE
          </Reveal>

          {/* Mission */}
          <Reveal delay={0.25}>
            <p className="text-white text-[15px] w-full max-w-[400px] leading-[2.1] mb-20 uppercase tracking-[0.13em] mx-auto opacity-80 font-light">
              We built this platform with a single purpose — to eliminate operational chaos and restore balance to your daily business routine
            </p>
          </Reveal>

          {/* Divider */}
          <Reveal
            delay={0.35}
            className="w-px h-16 mx-auto mb-20"
            style={{ background: 'linear-gradient(to bottom, transparent, rgba(255,255,255,0.4), transparent)' }}
          />

          {/* Signature */}
          <Reveal delay={0.45}>
            <div className="font-marck text-white leading-none mb-5" style={{ fontSize: 'clamp(68px, 14vw, 116px)' }}>
              S.P.D
            </div>
          </Reveal>

          <Reveal delay={0.5} className="font-manrope text-white/40 text-[9px] tracking-[0.42em] uppercase mb-24">
            Founder &amp; Chief Architect
          </Reveal>

          {/* Paragraphs */}
          <Reveal delay={0.58}>
            <div className="text-white w-full flex flex-col items-center font-light gap-10 mb-16">
              <p className="text-[15px] w-[420px] max-w-full text-center leading-[2] opacity-80">
                I Was Exhausted By Software That Demanded More Effort Than It Actually Saved. That Is Why We Engineered An Autonomous Architecture That Operates Silently In The Background.
              </p>
              <p className="text-[15px] w-[420px] max-w-full text-center leading-[2] opacity-80">
                Your Business Should Serve Your Life, Not Consume It. Let Our Algorithms Handle The Heavy Lifting, So You Can Focus On The Vision.
              </p>
            </div>
          </Reveal>

          {/* CTA */}
          <Reveal delay={0.65}>
            <a
              href="/login"
              className="inline-flex items-center gap-3 bg-white text-red-600 font-semibold text-sm px-8 py-4 rounded-full hover:bg-white/90 hover:scale-105 transition-all duration-200 shadow-lg"
            >
              Request Access
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </a>
          </Reveal>

        </div>
      </div>

      {/* ── Stats bar ───────────────────────────────────────────────────── */}
      <div className="relative z-10 w-full border-t border-white/10 bg-red-800/30 backdrop-blur-sm">
        <div className="max-w-3xl mx-auto flex flex-col md:flex-row items-center justify-around gap-8 py-12 px-8 text-center">
          {[
            { value: '250+', label: 'Brands Transformed' },
            { value: '95%',  label: 'Client Retention'   },
            { value: '10+',  label: 'Years in the Game'  },
          ].map(({ value, label }) => (
            <div key={label} className="flex flex-col items-center gap-2">
              <span className="font-italiana text-white text-4xl">{value}</span>
              <span className="font-manrope text-white/50 text-[11px] tracking-[0.3em] uppercase">{label}</span>
            </div>
          ))}
        </div>
      </div>

    </div>

    {/* ── Footer ──────────────────────────────────────────────────────── */}
    <Footer />
  )
}
