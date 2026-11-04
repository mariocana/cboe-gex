using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using VolSysAPI.Indicators;
using VolumetricaAPI.Chart;
using VolumetricaControls;

namespace GexIndicators
{
    /// <summary>
    /// Disegna i livelli GEX incollati dal sito nel parametro "Livelli".
    /// Deepchart non ammette file o rete in un indicatore: i dati arrivano solo da qui.
    /// Formato: GEX ES 2026-11-04 08:30|7628.50;tgt long = C3;S|7603.50;gamma flip;N
    /// </summary>
    public class GexLevels : Indicator
    {
        public static IndicatorDescriptionBase Register() =>
            new IndicatorDescriptionBase { Name = "GEX - Livelli" };

        [Category("Dati")]
        [DisplayName("Livelli (incolla dal sito)")]
        [VolCustom(CategoryIndex = 0, PropertyIndex = 0)]
        public string Livelli { get; set; } = "";

        [Category("Aspetto")]
        [DisplayName("Mostra le etichette")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 0)]
        public bool Etichette { get; set; } = true;

        [Category("Aspetto")]
        [DisplayName("Posizione etichette (0-1)")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 1, MinValue = 0, MaxValue = 1, IncrementValue = 0.05)]
        public double PosizioneEtichette { get; set; } = 0.02;

        [Category("Aspetto")]
        [DisplayName("Colore long")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 2)]
        public ColorRef ColoreLong { get; set; } = ColorRef.FromRgb(76, 175, 80);

        [Category("Aspetto")]
        [DisplayName("Colore short")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 3)]
        public ColorRef ColoreShort { get; set; } = ColorRef.FromRgb(224, 85, 85);

        [Category("Aspetto")]
        [DisplayName("Colore flip e gamma massimo")]
        [VolCustom(CategoryIndex = 1, PropertyIndex = 4)]
        public ColorRef ColoreNeutro { get; set; } = ColorRef.FromRgb(240, 192, 64);

        public override void OnSet(bool setDefault, bool themeOverride)
        {
            Description = "GEX";
        }

        public override void OnLoad()
        {
            var piano = Leggi(Livelli);
            StatusMessage = Avviso(piano, VAn.SymbolName);
            Description = piano.Titolo == "" ? "GEX" : piano.Titolo;
            foreach (var livello in piano.Righe)
                Disegna(livello);
        }

        private void Disegna(Livello livello)
        {
            ColorRef colore = livello.Tipo == "L" ? ColoreLong : livello.Tipo == "S" ? ColoreShort : ColoreNeutro;

            var linea = VAn.CreateAnnotation(AnnotationType.HorizontalLine);
            linea.X = 1;
            linea.Y = livello.Prezzo;
            linea.LineColor = colore;
            linea.LineWidth = 1;
            VAn.AddAnnotation(IndVars.Ann_List, linea);

            if (!Etichette)
                return;
            var testo = VAn.CreateAnnotation(AnnotationType.Text);
            testo.CoordinateXType = CoordinateTypeEnum.Relative;
            testo.X = PosizioneEtichette;
            testo.Y = livello.Prezzo;
            testo.Text = livello.Nome;
            testo.ForeColor = colore;
            VAn.AddAnnotation(IndVars.Ann_List, testo);
        }

        public static string Avviso(Piano piano, string simbolo)
        {
            if (piano.Righe.Count == 0)
                return "GEX: incolla i livelli dal sito nelle impostazioni dell'indicatore";
            if (piano.Scartate > 0)
                return "GEX: " + piano.Scartate + " righe non lette, ricopia i livelli dal sito";
            if (piano.Future != "" && !(simbolo ?? "").ToUpperInvariant().Contains(piano.Future))
                return "GEX: livelli " + piano.Future + " su " + simbolo + ", controlla il grafico";
            return null;
        }

        public static Piano Leggi(string testo)
        {
            var piano = new Piano();
            string[] pezzi = (testo ?? "").Split('|');
            for (int i = 0; i < pezzi.Length; i++)
            {
                string pezzo = pezzi[i].Trim();
                if (pezzo == "")
                    continue;
                if (i == 0 && pezzo.StartsWith("GEX "))
                {
                    piano.Titolo = pezzo;
                    string[] parole = pezzo.Split(' ');
                    piano.Future = parole.Length > 1 ? parole[1].ToUpperInvariant() : "";
                    continue;
                }
                string[] campi = pezzo.Split(';');
                if (campi.Length != 3)
                {
                    piano.Scartate++;
                    continue;
                }
                try
                {
                    double prezzo = Convert.ToDouble(campi[0].Trim(), CultureInfo.InvariantCulture);
                    piano.Righe.Add(new Livello { Prezzo = prezzo, Nome = campi[1].Trim(), Tipo = campi[2].Trim() });
                }
                catch (FormatException)
                {
                    piano.Scartate++;
                }
            }
            return piano;
        }
    }

    public class Livello
    {
        public double Prezzo;
        public string Nome = "";
        public string Tipo = "N";
    }

    public class Piano
    {
        public string Titolo = "";
        public string Future = "";
        public int Scartate;
        public List<Livello> Righe = new List<Livello>();
    }
}
