import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// The visual identity of the companion app.
///
/// Deliberately its own thing. Deep plum and saffron on warm cream,
/// generous spacing, rounded surfaces — the vocabulary of a travel app
/// someone opens on a crowded train, not a control room.

class Palette {
  static const plum = Color(0xFF6A2C4F);
  static const plumDark = Color(0xFF4A1D37);
  static const saffron = Color(0xFFE08A2E);
  static const cream = Color(0xFFFDFAF6);
  static const surface = Color(0xFFFFFFFF);
  static const sand = Color(0xFFF3EDE5);
  static const ink = Color(0xFF231C22);
  static const muted = Color(0xFF7A6E76);
  static const line = Color(0xFFE8DFD6);

  // Crowd bands. Never shown as numbers.
  static const clear = Color(0xFF2E7D63);
  static const moderate = Color(0xFFC98A1B);
  static const busy = Color(0xFFD4682F);
  static const packed = Color(0xFFB3352B);

  static Color crowd(String level) => switch (level) {
        'clear' => clear,
        'moderate' => moderate,
        'busy' => busy,
        _ => packed,
      };

  static String crowdLabel(String level) => switch (level) {
        'clear' => 'Clear',
        'moderate' => 'Steady',
        'busy' => 'Busy',
        _ => 'Very busy',
      };
}

ThemeData buildTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: Palette.plum,
    primary: Palette.plum,
    secondary: Palette.saffron,
    surface: Palette.surface,
    brightness: Brightness.light,
  );

  final text = GoogleFonts.manropeTextTheme().apply(
    bodyColor: Palette.ink,
    displayColor: Palette.ink,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: Palette.cream,
    textTheme: text.copyWith(
      headlineSmall: GoogleFonts.manrope(
        fontSize: 22, fontWeight: FontWeight.w700,
        letterSpacing: -0.4, color: Palette.ink,
      ),
      titleMedium: GoogleFonts.manrope(
        fontSize: 16, fontWeight: FontWeight.w600, color: Palette.ink,
      ),
      titleSmall: GoogleFonts.manrope(
        fontSize: 13.5, fontWeight: FontWeight.w600, color: Palette.ink,
      ),
      bodyMedium: GoogleFonts.manrope(
        fontSize: 14, height: 1.45, color: Palette.ink,
      ),
      bodySmall: GoogleFonts.manrope(
        fontSize: 12.5, height: 1.4, color: Palette.muted,
      ),
      labelSmall: GoogleFonts.manrope(
        fontSize: 10.5, fontWeight: FontWeight.w600,
        letterSpacing: 0.8, color: Palette.muted,
      ),
    ),
    appBarTheme: AppBarTheme(
      backgroundColor: Palette.cream,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
      centerTitle: false,
      titleTextStyle: GoogleFonts.manrope(
        fontSize: 20, fontWeight: FontWeight.w700,
        letterSpacing: -0.4, color: Palette.ink,
      ),
    ),
    cardTheme: CardThemeData(
      color: Palette.surface,
      elevation: 0,
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: const BorderSide(color: Palette.line),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: Palette.plum,
        foregroundColor: Colors.white,
        minimumSize: const Size(0, 50),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
        ),
        textStyle: GoogleFonts.manrope(
          fontSize: 15, fontWeight: FontWeight.w600,
        ),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: Palette.plum,
        minimumSize: const Size(0, 48),
        side: const BorderSide(color: Palette.line),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
        ),
        textStyle: GoogleFonts.manrope(
          fontSize: 14, fontWeight: FontWeight.w600,
        ),
      ),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: Palette.surface,
      indicatorColor: Palette.plum.withValues(alpha: 0.10),
      elevation: 0,
      height: 66,
      labelTextStyle: WidgetStatePropertyAll(
        GoogleFonts.manrope(fontSize: 11.5, fontWeight: FontWeight.w600),
      ),
    ),
    chipTheme: ChipThemeData(
      backgroundColor: Palette.sand,
      side: BorderSide.none,
      labelStyle: GoogleFonts.manrope(
        fontSize: 12.5, fontWeight: FontWeight.w600, color: Palette.ink,
      ),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
      ),
    ),
    dividerTheme: const DividerThemeData(
      color: Palette.line, thickness: 1, space: 1,
    ),
  );
}
