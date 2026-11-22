/-!
# A first look at categories

A category is a collection of *objects* together with *morphisms* (arrows)
between them. Every object has an identity arrow, and arrows that line up
end to start can be composed. Three laws must hold:

* composing with an identity on the left changes nothing,
* composing with an identity on the right changes nothing,
* composition is associative.

That's the whole definition. The power comes from how many different things
fit it: sets and functions, numbers and `≤`, a monoid viewed as a single
object, and so on. Mathlib has a full library for this; here we build the
definition by hand so every piece is visible.
-/

universe u v

namespace Playground

/-- A category with objects of type `Obj`. -/
class Category (Obj : Type u) where
  /-- The arrows from `X` to `Y`. -/
  Hom : Obj → Obj → Type v
  /-- Every object has an identity arrow. -/
  id : (X : Obj) → Hom X X
  /-- Arrows `X → Y` and `Y → Z` compose to an arrow `X → Z`. -/
  comp : {X Y Z : Obj} → Hom X Y → Hom Y Z → Hom X Z
  id_comp : ∀ {X Y : Obj} (f : Hom X Y), comp (id X) f = f
  comp_id : ∀ {X Y : Obj} (f : Hom X Y), comp f (id Y) = f
  assoc : ∀ {W X Y Z : Obj} (f : Hom W X) (g : Hom X Y) (h : Hom Y Z),
    comp (comp f g) h = comp f (comp g h)

/-! ## Example 1: types and functions

Objects are types, arrows are ordinary functions, identity is `fun x => x`,
and composition is "do `f`, then `g`". All three laws hold by `rfl`, because
both sides compute to the same function. -/

instance typesCategory : Category (Type u) where
  Hom X Y := X → Y
  id _ := fun x => x
  comp f g := fun x => g (f x)
  id_comp _ := rfl
  comp_id _ := rfl
  assoc _ _ _ := rfl

/-! ## Example 2: a single object

A category can have just one object. Then every arrow goes from that object
to itself, and the only interesting structure is how arrows compose.

Here the one object is `()`, the arrows are natural numbers, the identity
arrow is `0`, and composition is addition. The category laws turn into facts
about addition you've already met.

**Exercise:** replace each `sorry` below with a proof. Each one is a single
lemma about `Nat` addition. -/

instance natAddCategory : Category Unit where
  Hom _ _ := Nat
  id _ := 0
  comp f g := f + g
  id_comp f := by
    -- you need to show 0 + f = f
    sorry
  comp_id f := by
    -- you need to show f + 0 = f
    sorry
  assoc f g h := by
    -- you need to show (f + g) + h = f + (g + h)
    sorry

end Playground
