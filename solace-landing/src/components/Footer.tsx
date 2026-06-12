import { useRef } from 'react'
import { motion, useScroll, useTransform } from 'motion/react'
import { Facebook, Twitter, Instagram, Linkedin } from 'lucide-react'

/* ─── Data ──────────────────────────────────────────────────────────────── */

const NAV_COLS = [
  { title: 'Company',   items: ['Founding', 'Platform', 'Testify']             },
  { title: 'Mobile',    items: ['Get Apple App', 'Get Google App']              },
  { title: 'Contracts', items: ['Private Data', 'User Consent']                 },
]

const SOCIALS = [
  { Icon: Facebook,  label: 'Facebook'  },
  { Icon: Twitter,   label: 'Twitter'   },
  { Icon: Instagram, label: 'Instagram' },
  { Icon: Linkedin,  label: 'LinkedIn'  },
]

const BG_URL =
  'https://images.higgs.ai/?default=1&output=webp&url=https%3A%2F%2Fd8j0ntlcm91z4.cloudfront.net%2Fuser_38xzZboKViGWJOttwIXH07lWA1P%2Fhf_20260430_115327_3f256636-9e63-4885-8d0b-09317dc2b0a5.png&w=1280&q=85'

const TRUCK_URL =
  'https://roof-wish-40038865.figma.site/_components/v2/f31fd17907ce60745d45e83a61d44fd3810d5f25/truck_1.8c4bff83.png'

/* ─── Component ─────────────────────────────────────────────────────────── */

export default function Footer() {
  const containerRef = useRef<HTMLDivElement>(null)

  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ['start end', 'end start'],
  })

  const truckY = useTransform(scrollYProgress, [0, 1], [-50, 150])

  return (
    <div className="font-inter" style={{ backgroundColor: '#f8f9fa' }}>

      {/* ── Top spacer ────────────────────────────────────────────────── */}
      <section
        className="flex items-center justify-center h-[50vh] md:h-[30vh] lg:h-[50vh]"
        style={{ backgroundColor: '#FDFDFD' }}
      >
        <motion.p
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.9 }}
          className="text-gray-300 text-xs font-bold uppercase tracking-[0.5em]"
        >
          View Below
        </motion.p>
      </section>

      {/* ── Main parallax container ───────────────────────────────────── */}
      <section
        ref={containerRef}
        className="relative h-screen overflow-hidden bg-cover bg-center"
        style={{ backgroundImage: `url('${BG_URL}')` }}
      >

        {/* Footer card — top-aligned */}
        <div className="absolute top-0 w-full pt-12 md:pt-24 lg:pt-12 px-4 z-30">
          <motion.div
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, ease: 'easeOut' }}
            className="max-w-7xl mx-auto"
          >
            <div className="bg-white/95 backdrop-blur-sm shadow-xl rounded-2xl md:rounded-3xl overflow-hidden">

              {/* Top content */}
              <div className="flex flex-col md:flex-row md:justify-between gap-8 p-6 md:p-10">

                {/* Logo */}
                <div className="flex items-center gap-3 flex-shrink-0">
                  <div className="w-10 h-10 md:w-12 md:h-12 bg-orange-500 rounded-lg shadow-inner p-2 flex-shrink-0">
                    <svg viewBox="0 0 256 256" fill="white" className="w-full h-full">
                      <path d="M 228 0 C 172.772 0 128 44.772 128 100 L 128 0 L 0 0 L 0 28 C 0 83.228 44.772 128 100 128 L 0 128 L 0 256 L 28 256 C 83.228 256 128 211.228 128 156 L 128 256 L 256 256 L 256 228 C 256 172.772 211.228 128 156 128 L 256 128 L 256 0 Z" />
                    </svg>
                  </div>
                  <span className="text-gray-900 text-2xl md:text-3xl font-bold tracking-tighter">
                    HAUL!
                  </span>
                </div>

                {/* Link columns */}
                <div className="flex gap-8 md:gap-14 flex-wrap">
                  {NAV_COLS.map(({ title, items }) => (
                    <div key={title} className="flex flex-col gap-3">
                      <p className="text-sm font-bold uppercase tracking-widest text-gray-900">
                        {title}
                      </p>
                      {items.map(item => (
                        <a
                          key={item}
                          href="#"
                          className="text-gray-500 font-medium text-sm hover:text-orange-600 transition-colors duration-200"
                        >
                          {item}
                        </a>
                      ))}
                    </div>
                  ))}
                </div>
              </div>

              {/* Bottom bar */}
              <div className="border-t border-gray-100 bg-white px-6 md:px-10 py-4 flex items-center justify-between gap-4">
                <p className="text-sm text-gray-500 font-medium">
                  © 2026 HAUL! All Rights Reserved
                </p>
                <div className="flex items-center gap-2">
                  {SOCIALS.map(({ Icon, label }) => (
                    <a
                      key={label}
                      href="#"
                      aria-label={label}
                      className="w-10 h-10 rounded-full border border-gray-100 flex items-center justify-center text-gray-400 hover:bg-orange-500 hover:text-white hover:border-orange-500 transition-all duration-300"
                    >
                      <Icon className="w-5 h-5" />
                    </a>
                  ))}
                </div>
              </div>

            </div>
          </motion.div>
        </div>

        {/* ── Truck parallax layer ─────────────────────────────────────── */}
        <motion.div
          className="absolute inset-x-0 bottom-0 h-full pointer-events-none z-20"
          style={{ y: truckY }}
        >
          <img
            src={TRUCK_URL}
            alt="truck"
            className="w-full h-full object-contain object-bottom origin-bottom scale-[1.5] sm:scale-110 md:scale-[2.0] lg:scale-105"
          />
        </motion.div>

      </section>
    </div>
  )
}
