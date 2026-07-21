import '@testing-library/react'

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
//
// Note: the test-world locale is pinned to English at the source via
// resolveDefaultLocale() in src/i18n/languages.ts (ADR-V6-077), so no runtime
// pin is needed here.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true
